"""Telegram HTML message builders. All user-supplied text is escaped."""
from __future__ import annotations

import html
from typing import List

from .match_service import STATUS_LABEL, format_time, is_team_match
from .registration_service import compress_ranges

e = html.escape
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
TG_LIMIT = 4000


def split_message(text: str, limit: int = TG_LIMIT) -> List[str]:
    """Split on line boundaries so no message exceeds Telegram's 4096 limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def mode_label(m) -> str:
    if m["mode"] == "team" and m["team_size"] in (2, 4):
        return "Duo" if m["team_size"] == 2 else "Squad"
    base = m["mode"].capitalize()
    if is_team_match(m):
        return f"{base} ({m['team_count']} teams x {m['team_size']})"
    return base


def announcement_text(m, registered: int) -> str:
    """Shared match announcement: counts always come from the database, remaining is never negative."""
    remaining = max(0, m["capacity"] - registered)
    if m["status"] == "registration_open":
        status = "🔴 Full" if remaining == 0 else "🟢 Registration Open"
    else:
        status = STATUS_LABEL[m["status"]]
    lines = ["🏆 <b>NEW MATCH CREATED!</b>", "", f"Match: #{m['id']} — {e(m['name'])}",
             f"Mode: {e(mode_label(m))}",
             f"Time: {format_time(m['start_utc'], m['timezone'])} ({e(m['timezone'])})",
             f"Registered: {registered}/{m['capacity']}", f"Slots remaining: {remaining}",
             f"Status: {status}"]
    if m["status"] == "registration_open" and remaining > 0:
        lines += ["", f"Join using <code>/joinmatch {m['id']}</code> too."]
    return "\n".join(lines)


def match_card(m, registered: int = None) -> str:
    lines = [f"🎮 <b>{e(m['name'])}</b>  (ID {m['id']})",
             f"Mode: {e(mode_label(m))}",
             f"Time: {format_time(m['start_utc'], m['timezone'])} ({e(m['timezone'])})",
             f"Status: {STATUS_LABEL[m['status']]}",
             f"Capacity: {m['capacity']}"]
    if registered is not None:
        lines.append(f"Registered: {registered}/{m['capacity']}")
    if m["reg_deadline_utc"]:
        lines.append(f"Registration deadline: {format_time(m['reg_deadline_utc'], m['timezone'])}")
    return "\n".join(lines)


def registration_success(reg, m) -> str:
    return (f"✅ <b>REGISTRATION SUCCESSFUL!</b>\n\n"
            f"👤 Player: {e(reg['ign'])}\n🆔 UID: {reg['uid']}\n🎮 Match: {e(m['name'])}\n"
            f"🎯 Slot: <b>{reg['slot']}</b>\n"
            f"⏰ Match Time: {format_time(m['start_utc'], m['timezone'])} ({e(m['timezone'])})")


def my_registration(reg, m) -> str:
    return registration_success(reg, m).replace("REGISTRATION SUCCESSFUL!", "YOUR REGISTRATION")


def slots_page(view: dict, page: int, per_page: int = 20):
    m, regs = view["match"], view["regs"]
    pages = max(1, -(-len(regs) // per_page))
    page = min(max(page, 1), pages)
    chunk = regs[(page - 1) * per_page: page * per_page]
    out = [f"🔥 <b>FREE FIRE CUSTOM ROOM</b>", f"Match ID: {m['id']} — {e(m['name'])}",
           f"Mode: {e(mode_label(m))}", f"Time: {format_time(m['start_utc'], m['timezone'], False)}", "",
           f"Total Slots: {view['total']}", f"Registered Players: {view['registered']}",
           f"Occupied Slots: {view['occupied']}", f"Empty Slots: {view['empty']}"]
    if view["over_capacity"]:
        out.append(f"⚠️ {view['over_capacity']} registration(s) exceed the current capacity (kept, not deleted).")
    out += ["", "<b>Occupied:</b>"]
    out += [f"Slot {r['slot']} - {e(r['ign'])}" for r in chunk] or ["—"]
    if page == 1:
        out += ["", "<b>Empty:</b>", compress_ranges(view["empty_slots"]) or "None"]
    out.append(f"\nPage {page}/{pages}")
    return "\n".join(out), page, pages


def teams_page(view: dict, page: int, per_page: int = 6):
    m, teams = view["match"], view["teams"]
    if not teams:
        return "This match has no teams.", 1, 1
    pages = max(1, -(-len(teams) // per_page))
    page = min(max(page, 1), pages)
    out = [f"👥 <b>TEAMS — {e(m['name'])}</b> (ID {m['id']})", f"Team size: {m['team_size']}", ""]
    for t in teams[(page - 1) * per_page: page * per_page]:
        mem = t["members"]
        out.append(f"<b>{e(t['team']['name'])}</b> — {len(mem)}/{m['team_size']} "
                   f"({m['team_size'] - len(mem)} open) → /jointeam{t['team']['number']}")
        out += [f"  • {e(x['ign'])} (slot {x['slot']})" for x in mem]
    if page == pages and view["unassigned"]:
        out += ["", f"Not in a team: {len(view['unassigned'])} player(s)"]
    out.append(f"\nPage {page}/{pages}")
    return "\n".join(out), page, pages


def leaderboard(title: str, snap: dict, rows: List[dict], *, status_note: str = "",
                cumulative: bool = False) -> str:
    out = [f"🏆 <b>{e(title)}</b>"]
    if status_note:
        out.append(status_note)
    out.append("")
    if not rows:
        out.append("No results yet.")
    for r in rows:
        medal = MEDALS.get(r["rank"], f"{r['rank']}.")
        if cumulative:
            out.append(f"{medal} {e(r['name'])} — <b>{r['total']}</b> pts | {r['kills']} kills | "
                       f"{r['matches']} match(es)")
        else:
            out.append(f"{medal} {e(r['name'])} — <b>{r['total']}</b> pts "
                       f"(P{r['placement']}: {r['placement_points']} + {r['kills']}K: {r['kill_points']})")
    if snap and snap.get("player_kills"):
        out += ["", "🎯 <b>Individual kills</b>"]
        out += [f"{e(p['name'])} — {p['kills']}" for p in snap["player_kills"][:20]]
    return "\n".join(out)


def audit_line(a) -> str:
    return f"{a['created_at']} | admin {a['admin_id']} | {a['action']} | {e(a['target'] or '')} {e(a['details'] or '')}"
