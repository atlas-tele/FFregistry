"""Team commands. /jointeamN is generated dynamically from the match configuration."""
from __future__ import annotations

import re

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from keyboards.player_menu import pager
from services import formatting as fmt
from services.errors import ServiceError
from .announcements import notify_team_join
from .common import App, e, group_command, player_match, reply

JOIN_RE = re.compile(r"^/jointeam(\d+)(?:@\w+)?(?:\s|$)", re.IGNORECASE)


@group_command
async def teams(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    text, page, pages = fmt.teams_page(app.ts.view(m["id"]), 1)
    await reply(update, text, pager("teams", m["id"], page, pages))


@group_command
async def myteam(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    if not m["team_count"]:
        await reply(update, "This match has no teams.")
        return
    reg, team = app.ts.team_of(m["id"], update.effective_user.id)
    if reg is None:
        await reply(update, "You are not registered yet. Use /register.")
    elif team is None:
        await reply(update, f"You are not in a team yet. Use /jointeam1 … /jointeam{m['team_count']}.")
    else:
        mem = "\n".join(f"• {e(x['ign'])} (slot {x['slot']})" for x in team["members"])
        await reply(update, f"👥 <b>{e(team['team']['name'])}</b> — {len(team['members'])}/{m['team_size']}\n{mem}")


@group_command
async def jointeam(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    text = update.effective_message.text or ""
    match = JOIN_RE.match(text)
    if match:
        number = int(match.group(1))
    elif context.args and context.args[0].isdigit():  # "/jointeam 3" also works
        number = int(context.args[0])
    else:
        raise ServiceError("Use /jointeam1, /jointeam2 … (see /teams).")
    m = app.ms.active_for_group(update.effective_chat.id)
    if m is None:
        raise ServiceError("There is no active match in this group right now.")
    team, reg = app.ts.join(m["id"], update.effective_user.id, number)
    view = app.ts.view(m["id"])
    cur = next(t for t in view["teams"] if t["team"]["id"] == team["id"])
    await reply(update, f"✅ {e(reg['ign'])} joined <b>{e(team['name'])}</b> "
                        f"({len(cur['members'])}/{m['team_size']}).")
    await notify_team_join(context.bot, app, reg, m, team)


@group_command
async def leaveteam(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, [])
    team = app.ts.leave(m["id"], update.effective_user.id)
    await reply(update, f"👋 You left <b>{e(team['name'])}</b>.")


def handlers():
    return [
        CommandHandler("teams", teams), CommandHandler("myteam", myteam),
        CommandHandler("leaveteam", leaveteam), CommandHandler("jointeam", jointeam),
        MessageHandler(filters.COMMAND & filters.Regex(JOIN_RE), jointeam),
    ]
