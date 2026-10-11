"""/points — leaderboards built from PUBLISHED snapshots only."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from services import formatting as fmt
from services.errors import ServiceError
from services.match_service import DONE, PROGRESS
from .common import App, e, group_command, reply


def _published_match(app: App, group_id: int, want_team=None, match_id=None):
    """Return (match, snapshot) for the first suitable match with published results."""
    if match_id is not None:
        m = app.ms.get_for_group(match_id, group_id)
        return m, app.res.published_snapshot(m["id"])
    candidates = []
    active = app.ms.active_for_group(group_id)
    if active:
        candidates.append(active)
    candidates += [m for m in app.ms.list(group_id, [DONE, PROGRESS], 50) if not active or m["id"] != active["id"]]
    for m in candidates:
        snap = app.res.published_snapshot(m["id"])
        if snap and (want_team is None or snap["team"] == want_team):
            return m, snap
    return None, None


def render(app: App, m, snap) -> str:
    title = f"{m['name']} — Match #{m['id']}"
    return fmt.leaderboard(title, snap, snap["rows"], status_note="✅ <i>Official, published results</i>")


@group_command
async def points(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    gid = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg == "all":
        rows = app.res.cumulative(gid)
        if not rows:
            await reply(update, "No completed matches with published results yet.")
            return
        await reply(update, fmt.leaderboard("Cumulative leaderboard (completed matches)", {}, rows, cumulative=True)
                    + "\n\n<i>Players are matched across matches by Free Fire UID. In team matches every team "
                      "member receives the team's points once.</i>")
        return
    if arg.isdigit():
        m, snap = _published_match(app, gid, match_id=int(arg))
        if not snap:
            await reply(update, f"Results for match #{m['id']} have not been published yet.")
            return
    elif arg in ("solo", "team"):
        m, snap = _published_match(app, gid, want_team=(arg == "team"))
        if not snap:
            await reply(update, f"No published {arg} results yet.")
            return
    elif arg == "":
        m, snap = _published_match(app, gid)
        if not snap:
            await reply(update, "No results have been published yet.")
            return
    else:
        raise ServiceError("Usage: /points [match_id | solo | team | all]")
    await reply(update, render(app, m, snap))
