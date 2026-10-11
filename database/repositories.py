"""Plain SQL helpers. Every function takes an open connection so that callers
(the services) decide transaction boundaries."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

Row = sqlite3.Row

MATCH_COLUMNS = {
    "name", "mode", "start_utc", "timezone", "capacity", "team_count", "team_size",
    "status", "reg_deadline_utc", "allow_cancel", "allow_switch",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- groups -------------------------------------------------------------
def upsert_group(conn, chat_id: int, title: str, authorized: Optional[bool] = None) -> None:
    conn.execute(
        "INSERT INTO groups (chat_id, title, authorized, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
        (chat_id, title or "", 1 if authorized else 0, now_iso()),
    )
    if authorized is not None:
        conn.execute("UPDATE groups SET authorized=? WHERE chat_id=?", (1 if authorized else 0, chat_id))


def get_group(conn, chat_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)).fetchone()


def list_groups(conn) -> list:
    return conn.execute("SELECT * FROM groups ORDER BY authorized DESC, created_at").fetchall()


# ---- admins -------------------------------------------------------------
def add_admin(conn, user_id: int, added_by: int) -> None:
    conn.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?,?,?)",
                 (user_id, added_by, now_iso()))


def remove_admin(conn, user_id: int) -> int:
    return conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,)).rowcount


def list_admin_ids(conn) -> list:
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM admins ORDER BY added_at")]


# ---- matches ------------------------------------------------------------
def insert_match(conn, **f: Any) -> int:
    cols = ["group_id", "name", "mode", "start_utc", "timezone", "capacity", "team_count",
            "team_size", "reg_deadline_utc", "allow_cancel", "allow_switch", "created_by"]
    values = [f.get(c) for c in cols]
    cur = conn.execute(
        f"INSERT INTO matches ({','.join(cols)}, created_at) VALUES ({','.join('?' * len(cols))}, ?)",
        values + [now_iso()],
    )
    return int(cur.lastrowid)


def get_match(conn, match_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()


def update_match(conn, match_id: int, **fields: Any) -> None:
    bad = set(fields) - MATCH_COLUMNS
    if bad:
        raise ValueError(f"Cannot update columns: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE matches SET {sets} WHERE id=?", [*fields.values(), match_id])


def list_matches(conn, group_id: Optional[int] = None, statuses: Optional[Iterable[str]] = None,
                 limit: int = 50) -> list:
    q, args = "SELECT * FROM matches WHERE 1=1", []
    if group_id is not None:
        q += " AND group_id=?"
        args.append(group_id)
    if statuses:
        st = list(statuses)
        q += f" AND status IN ({','.join('?' * len(st))})"
        args += st
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


# ---- scoring rules ------------------------------------------------------
def set_rules(conn, match_id: int, placement_json: str, kill_points: int) -> None:
    conn.execute(
        "INSERT INTO scoring_rules (match_id, placement_json, kill_points) VALUES (?,?,?) "
        "ON CONFLICT(match_id) DO UPDATE SET placement_json=excluded.placement_json, "
        "kill_points=excluded.kill_points", (match_id, placement_json, kill_points))


def get_rules_row(conn, match_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM scoring_rules WHERE match_id=?", (match_id,)).fetchone()


# ---- registrations ------------------------------------------------------
def insert_registration(conn, match_id, telegram_id, username, ign, uid, slot) -> int:
    cur = conn.execute(
        "INSERT INTO registrations (match_id, telegram_id, username, ign, uid, slot, created_at) "
        "VALUES (?,?,?,?,?,?,?)", (match_id, telegram_id, username, ign, uid, slot, now_iso()))
    return int(cur.lastrowid)


def get_registration(conn, reg_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()


def get_reg_by_user(conn, match_id: int, telegram_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM registrations WHERE match_id=? AND telegram_id=?",
                        (match_id, telegram_id)).fetchone()


def get_reg_by_uid(conn, match_id: int, uid: str) -> Optional[Row]:
    return conn.execute("SELECT * FROM registrations WHERE match_id=? AND uid=?",
                        (match_id, uid)).fetchone()


def get_reg_by_slot(conn, match_id: int, slot: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM registrations WHERE match_id=? AND slot=?",
                        (match_id, slot)).fetchone()


def list_registrations(conn, match_id: int) -> list:
    return conn.execute("SELECT * FROM registrations WHERE match_id=? ORDER BY slot",
                        (match_id,)).fetchall()


def count_registrations(conn, match_id: int) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM registrations WHERE match_id=?",
                        (match_id,)).fetchone()["c"]


def search_registrations(conn, match_id: int, term: str, limit: int = 20) -> list:
    like = "%" + term.replace("%", "").replace("_", "") + "%"
    return conn.execute(
        "SELECT * FROM registrations WHERE match_id=? AND (uid LIKE ? OR ign LIKE ?) "
        "ORDER BY slot LIMIT ?", (match_id, like, like, limit)).fetchall()


def delete_registration(conn, reg_id: int) -> int:
    return conn.execute("DELETE FROM registrations WHERE id=?", (reg_id,)).rowcount


def update_slot(conn, reg_id: int, slot: int) -> None:
    conn.execute("UPDATE registrations SET slot=? WHERE id=?", (slot, reg_id))


# ---- teams --------------------------------------------------------------
def insert_team(conn, match_id: int, number: int, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO teams (match_id, number, name) VALUES (?,?,?)",
                 (match_id, number, name))


def get_team(conn, match_id: int, number: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM teams WHERE match_id=? AND number=?",
                        (match_id, number)).fetchone()


def get_team_by_id(conn, team_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()


def list_teams(conn, match_id: int) -> list:
    return conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM team_memberships m WHERE m.team_id=t.id) AS members "
        "FROM teams t WHERE t.match_id=? ORDER BY t.number", (match_id,)).fetchall()


def delete_teams_above(conn, match_id: int, number: int) -> None:
    conn.execute("DELETE FROM teams WHERE match_id=? AND number>?", (match_id, number))


def team_members(conn, team_id: int) -> list:
    return conn.execute(
        "SELECT r.* FROM team_memberships m JOIN registrations r ON r.id=m.registration_id "
        "WHERE m.team_id=? ORDER BY r.slot", (team_id,)).fetchall()


def count_team_members(conn, team_id: int) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM team_memberships WHERE team_id=?",
                        (team_id,)).fetchone()["c"]


def get_membership(conn, match_id: int, reg_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM team_memberships WHERE match_id=? AND registration_id=?",
                        (match_id, reg_id)).fetchone()


def add_membership(conn, match_id: int, team_id: int, reg_id: int) -> None:
    conn.execute("INSERT INTO team_memberships (match_id, team_id, registration_id, joined_at) "
                 "VALUES (?,?,?,?)", (match_id, team_id, reg_id, now_iso()))


def move_membership(conn, match_id: int, reg_id: int, team_id: int) -> None:
    conn.execute("UPDATE team_memberships SET team_id=?, joined_at=? WHERE match_id=? AND registration_id=?",
                 (team_id, now_iso(), match_id, reg_id))


def delete_membership(conn, match_id: int, reg_id: int) -> int:
    return conn.execute("DELETE FROM team_memberships WHERE match_id=? AND registration_id=?",
                        (match_id, reg_id)).rowcount


def count_memberships(conn, match_id: int) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM team_memberships WHERE match_id=?",
                        (match_id,)).fetchone()["c"]


def max_team_fill(conn, match_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(c),0) AS m FROM (SELECT COUNT(*) AS c FROM team_memberships "
        "WHERE match_id=? GROUP BY team_id)", (match_id,)).fetchone()
    return row["m"]


# ---- results ------------------------------------------------------------
def list_results(conn, match_id: int) -> list:
    return conn.execute("SELECT * FROM results WHERE match_id=? ORDER BY placement",
                        (match_id,)).fetchall()


def count_results(conn, match_id: int) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM results WHERE match_id=?",
                        (match_id,)).fetchone()["c"]


def get_result_for(conn, match_id: int, reg_id: Optional[int], team_id: Optional[int]) -> Optional[Row]:
    if reg_id is not None:
        return conn.execute("SELECT * FROM results WHERE match_id=? AND registration_id=?",
                            (match_id, reg_id)).fetchone()
    return conn.execute("SELECT * FROM results WHERE match_id=? AND team_id=?",
                        (match_id, team_id)).fetchone()


def get_result_by_placement(conn, match_id: int, placement: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM results WHERE match_id=? AND placement=?",
                        (match_id, placement)).fetchone()


def insert_result(conn, match_id, reg_id, team_id, placement, kills, pp, kp, total, admin_id) -> None:
    conn.execute(
        "INSERT INTO results (match_id, registration_id, team_id, placement, kills, placement_points, "
        "kill_points, total, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (match_id, reg_id, team_id, placement, kills, pp, kp, total, admin_id, now_iso()))


def update_result(conn, result_id, placement, kills, pp, kp, total, admin_id) -> None:
    conn.execute(
        "UPDATE results SET placement=?, kills=?, placement_points=?, kill_points=?, total=?, "
        "updated_by=?, updated_at=? WHERE id=?",
        (placement, kills, pp, kp, total, admin_id, now_iso(), result_id))


def delete_result(conn, result_id: int) -> None:
    conn.execute("DELETE FROM results WHERE id=?", (result_id,))


def set_player_kills(conn, match_id: int, reg_id: int, kills: int) -> None:
    conn.execute(
        "INSERT INTO player_kills (match_id, registration_id, kills) VALUES (?,?,?) "
        "ON CONFLICT(match_id, registration_id) DO UPDATE SET kills=excluded.kills",
        (match_id, reg_id, kills))


def list_player_kills(conn, match_id: int) -> list:
    return conn.execute(
        "SELECT r.ign, r.uid, p.kills FROM player_kills p JOIN registrations r ON r.id=p.registration_id "
        "WHERE p.match_id=? ORDER BY p.kills DESC, r.ign", (match_id,)).fetchall()


# ---- published ----------------------------------------------------------
def upsert_published(conn, match_id, chat_id, message_id, admin_id, snapshot: str) -> None:
    conn.execute(
        "INSERT INTO published_results (match_id, chat_id, message_id, published_at, published_by, snapshot) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET chat_id=excluded.chat_id, "
        "message_id=excluded.message_id, published_at=excluded.published_at, "
        "published_by=excluded.published_by, snapshot=excluded.snapshot",
        (match_id, chat_id, message_id, now_iso(), admin_id, snapshot))


def get_published(conn, match_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM published_results WHERE match_id=?", (match_id,)).fetchone()


# ---- audit --------------------------------------------------------------
def add_audit(conn, admin_id: int, match_id: Optional[int], action: str,
              target: str = "", details: str = "") -> None:
    conn.execute("INSERT INTO audit_logs (admin_id, match_id, action, target, details, created_at) "
                 "VALUES (?,?,?,?,?,?)", (admin_id, match_id, action, target, details, now_iso()))


def list_audit(conn, match_id: Optional[int] = None, limit: int = 20) -> list:
    if match_id is None:
        return conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return conn.execute("SELECT * FROM audit_logs WHERE match_id=? ORDER BY id DESC LIMIT ?",
                        (match_id, limit)).fetchall()


# ---- announcements (one editable message per match and authorized group) -----
def get_announcement(conn, match_id: int, chat_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM match_announcements WHERE match_id=? AND chat_id=?",
                        (match_id, chat_id)).fetchone()


def upsert_announcement(conn, match_id: int, chat_id: int, message_id: int) -> None:
    conn.execute(
        "INSERT INTO match_announcements (match_id, chat_id, message_id, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(match_id, chat_id) DO UPDATE SET message_id=excluded.message_id, "
        "updated_at=excluded.updated_at", (match_id, chat_id, message_id, now_iso()))


def latest_registration_for_user(conn, telegram_id: int) -> Optional[Row]:
    return conn.execute("SELECT * FROM registrations WHERE telegram_id=? ORDER BY id DESC LIMIT 1",
                        (telegram_id,)).fetchone()
