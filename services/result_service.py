"""Result entry, editing, snapshots and cumulative leaderboards.

Results are ONLY what an administrator types in. Nothing is fetched from the game.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from database import Database
from database import repositories as repo
from .errors import ServiceError
from .match_service import PROGRESS, DONE, is_team_match, MatchService
from .scoring_service import MAX_KILLS, Rules, calculate, rank_rows, aggregate


def recalc_scores(conn, match_id: int, rules: Rules) -> None:
    for r in repo.list_results(conn, match_id):
        s = calculate(rules, r["placement"], r["kills"])
        repo.update_result(conn, r["id"], r["placement"], r["kills"], s.placement_points,
                           s.kill_points, s.total, r["updated_by"])


def _rules(conn, match_id: int) -> Rules:
    row = repo.get_rules_row(conn, match_id)
    return Rules.from_json(row["placement_json"], row["kill_points"])


def max_placement(match) -> int:
    return match["team_count"] if is_team_match(match) else match["capacity"]


def resolve_target(conn, match, token: str):
    """UID (digits) -> player; 'T3' -> team 3. Returns (reg_id, team_id, label)."""
    token = (token or "").strip()
    if is_team_match(match):
        if not (token[:1] in ("t", "T") and token[1:].isdigit()):
            raise ServiceError("This is a team match. Use the team token, e.g. T1.", "bad_target")
        team = repo.get_team(conn, match["id"], int(token[1:]))
        if not team:
            raise ServiceError("Team not found.", "bad_target")
        return None, team["id"], team["name"]
    reg = repo.get_reg_by_uid(conn, match["id"], token)
    if not reg:
        raise ServiceError("No registered player with that UID in this match.", "bad_target")
    return reg["id"], None, reg["ign"]


class ResultService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def participants(self, match_id: int) -> List[dict]:
        with self.db.read() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            out = []
            if is_team_match(m):
                for t in repo.list_teams(conn, match_id):
                    if t["members"]:
                        res = repo.get_result_for(conn, match_id, None, t["id"])
                        out.append({"key": f"T{t['number']}", "name": t["name"], "done": bool(res)})
            else:
                for r in repo.list_registrations(conn, match_id):
                    res = repo.get_result_for(conn, match_id, r["id"], None)
                    out.append({"key": r["uid"], "name": r["ign"], "done": bool(res)})
        return out

    def preview(self, match_id: int, token: str, placement: int, kills: int) -> dict:
        m = MatchService(self.db).get(match_id)
        self._validate(m, placement, kills)
        score = calculate(MatchService(self.db).rules(match_id), placement, kills)
        return {"score": score}

    @staticmethod
    def _validate(m, placement: int, kills: int) -> None:
        if m["status"] not in (PROGRESS, DONE):
            raise ServiceError("Results can be entered once the match is in progress (/startmatch) or completed.")
        top = max_placement(m)
        if not 1 <= placement <= top:
            raise ServiceError(f"Placement must be between 1 and {top}.", "bad_placement")
        if not 0 <= kills <= MAX_KILLS:
            raise ServiceError(f"Kills must be between 0 and {MAX_KILLS}.", "bad_kills")

    def save(self, admin_id: int, match_id: int, token: str, placement: int, kills: int,
             overwrite: bool = False):
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            self._validate(m, placement, kills)
            reg_id, team_id, label = resolve_target(conn, m, token)
            rules = _rules(conn, match_id)
            score = calculate(rules, placement, kills)
            existing = repo.get_result_for(conn, match_id, reg_id, team_id)
            clash = repo.get_result_by_placement(conn, match_id, placement)
            if clash and (not existing or clash["id"] != existing["id"]):
                raise ServiceError(f"Placement {placement} is already assigned to another participant.", "dup_placement")
            if existing and not overwrite:
                raise ServiceError(f"{label} already has a result. Use /editresult to change it.", "duplicate")
            if not existing and overwrite:
                raise ServiceError(f"{label} has no result yet. Use the submit flow first.", "no_result")
            if existing:
                repo.update_result(conn, existing["id"], placement, kills, score.placement_points,
                                   score.kill_points, score.total, admin_id)
                action = "result_edit"
            else:
                repo.insert_result(conn, match_id, reg_id, team_id, placement, kills,
                                   score.placement_points, score.kill_points, score.total, admin_id)
                action = "result_add"
            repo.add_audit(conn, admin_id, match_id, action, token, f"p{placement} k{kills} = {score.total}")
            return label, score

    def delete(self, admin_id: int, match_id: int, token: str) -> str:
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            reg_id, team_id, label = resolve_target(conn, m, token)
            ex = repo.get_result_for(conn, match_id, reg_id, team_id)
            if not ex:
                raise ServiceError(f"{label} has no result.", "no_result")
            repo.delete_result(conn, ex["id"])
            repo.add_audit(conn, admin_id, match_id, "result_delete", token, label)
            return label

    def save_player_kills(self, admin_id: int, match_id: int, uid: str, kills: int) -> str:
        if not 0 <= kills <= MAX_KILLS:
            raise ServiceError(f"Kills must be between 0 and {MAX_KILLS}.")
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            if not is_team_match(m):
                raise ServiceError("Individual kill stats are only used in team matches.")
            reg = repo.get_reg_by_uid(conn, match_id, uid.strip())
            if not reg:
                raise ServiceError("No registered player with that UID.", "bad_target")
            repo.set_player_kills(conn, match_id, reg["id"], kills)
            repo.add_audit(conn, admin_id, match_id, "player_kills", uid, str(kills))
            return reg["ign"]

    # -- leaderboards -------------------------------------------------
    def live_snapshot(self, match_id: int) -> dict:
        with self.db.read() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            team = is_team_match(m)
            rows = []
            for r in repo.list_results(conn, match_id):
                if team:
                    t = repo.get_team_by_id(conn, r["team_id"])
                    members = [{"uid": x["uid"], "name": x["ign"]} for x in repo.team_members(conn, t["id"])]
                    name, key = t["name"], f"T{t['number']}"
                else:
                    reg = repo.get_registration(conn, r["registration_id"])
                    members = [{"uid": reg["uid"], "name": reg["ign"]}]
                    name, key = reg["ign"], reg["uid"]
                rows.append({"key": key, "name": name, "placement": r["placement"], "kills": r["kills"],
                             "placement_points": r["placement_points"], "kill_points": r["kill_points"],
                             "total": r["total"], "members": members})
            pk = [{"name": x["ign"], "uid": x["uid"], "kills": x["kills"]} for x in repo.list_player_kills(conn, match_id)]
        return {"match_id": match_id, "mode": m["mode"], "team": team,
                "rows": rank_rows(rows), "player_kills": pk}

    def published_snapshot(self, match_id: int) -> Optional[dict]:
        with self.db.read() as conn:
            p = repo.get_published(conn, match_id)
        if not p:
            return None
        snap = json.loads(p["snapshot"])
        snap["published_at"] = p["published_at"]
        return snap

    def has_unpublished_changes(self, match_id: int) -> bool:
        pub = self.published_snapshot(match_id)
        if pub is None:
            return True
        live = self.live_snapshot(match_id)
        return (json.dumps(live["rows"], sort_keys=True) != json.dumps(pub["rows"], sort_keys=True)
                or json.dumps(live["player_kills"], sort_keys=True) != json.dumps(pub["player_kills"], sort_keys=True))

    def record_publish(self, admin_id: int, match_id: int, chat_id: int, message_id: Optional[int],
                       snapshot: dict) -> None:
        with self.db.transaction() as conn:
            repo.upsert_published(conn, match_id, chat_id, message_id, admin_id, json.dumps(snapshot))
            repo.add_audit(conn, admin_id, match_id, "results_publish", str(chat_id), "")

    def cumulative(self, group_id: int) -> List[dict]:
        """Published results of all COMPLETED matches (matches are shared by every authorized
        group), matched by UID. `group_id` is kept for call compatibility."""
        snaps = []
        with self.db.read() as conn:
            for m in reversed(repo.list_matches(conn, None, [DONE], 200)):
                p = repo.get_published(conn, m["id"])
                if p:
                    snaps.append(json.loads(p["snapshot"]))
        return aggregate(snaps)
