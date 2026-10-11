"""Inline keyboards for ordinary group members."""
from __future__ import annotations

from typing import List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def pager(kind: str, match_id: int, page: int, pages: int):
    if pages <= 1:
        return None
    row: List[InlineKeyboardButton] = []
    if page > 1:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"pg:{kind}:{match_id}:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="pg:noop:0:0"))
    if page < pages:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"pg:{kind}:{match_id}:{page + 1}"))
    return InlineKeyboardMarkup([row])


def join_keyboard(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Join Match", callback_data=f"join:{match_id}")]])


def confirm_registration() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="reg:ok"),
        InlineKeyboardButton("✏️ Re-enter", callback_data="reg:redo"),
        InlineKeyboardButton("❌ Cancel", callback_data="reg:no"),
    ]])


PLAYER_HELP = (
    "🔥 <b>Free Fire Custom Room Bot</b>\n\n"
    "<b>Players (use in the group)</b>\n"
    "/register — join the active match\n"
    "/joinmatch &lt;id&gt; — join a specific match\n"
    "/myregistration — your slot and details\n"
    "/cancelregistration — withdraw (if allowed)\n"
    "/slots [id] — slot list · /count — counts\n"
    "/teams [id] · /myteam · /leaveteam\n"
    "{team_cmds}"
    "/matches · /activematch\n"
    "/resultmatch &lt;id&gt; — published results\n"
    "/points [id|solo|team|all] — leaderboards\n"
    "/cancel — stop the current step\n"
)
