"""Match listing for players, lifecycle commands for admins."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from keyboards.admin_panel import confirm
from services import formatting as fmt
from services.errors import ServiceError
from services.match_service import MODES, STATUS_LABEL
from .announcements import after_status_change, refresh_announcements
from .common import (App, admin_command, e, group_command, pick_admin_match, reply, split_match_arg)


@group_command
async def matches(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    rows = [m for m in app.ms.list(None, limit=15) if m["status"] != "draft"]
    if not rows:
        await reply(update, "No matches have been announced yet.")
        return
    lines = ["📋 <b>Matches</b>"]
    for m in rows:
        lines.append(f"#{m['id']} · {e(m['name'])} · {e(m['mode'].capitalize())} · "
                     f"{fmt.format_time(m['start_utc'], m['timezone'])} · {STATUS_LABEL[m['status']]}")
    await reply(update, "\n".join(lines))


@group_command
async def activematch(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = app.ms.active_for_group(update.effective_chat.id)
    if m is None:
        await reply(update, "There is no active match right now.")
        return
    await reply(update, fmt.match_card(m, app.rs.slots_view(m["id"])["registered"]))


# ---- admin lifecycle ---------------------------------------------------
async def _transition(update, context, app: App, action: str, label: str) -> None:
    mid, _ = split_match_arg(context.args or [], 0)
    m = pick_admin_match(app, context, mid)
    if action in ("end", "cancel") and context.args != ["confirm"]:
        code = "endm" if action == "end" else "cancelm"
        await reply(update, f"⚠️ {label} <b>{e(m['name'])}</b> (ID {m['id']})?", confirm(f"adm:y:{code}:{m['id']}"))
        return
    new = app.ms.transition(update.effective_user.id, m["id"], action)
    await reply(update, f"✅ <b>{e(new['name'])}</b> (ID {new['id']}) → {STATUS_LABEL[new['status']]}")
    await after_status_change(context.bot, app, new)


@admin_command
async def open_reg(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await _transition(update, context, app, "open", "Open registration for")


@admin_command
async def close_reg(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await _transition(update, context, app, "close", "Close registration for")


@admin_command
async def start_match(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await _transition(update, context, app, "start", "Start")


@admin_command
async def end_match(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await _transition(update, context, app, "end", "End")


@admin_command
async def cancel_match(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await _transition(update, context, app, "cancel", "Cancel")


@admin_command
async def select_match(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        raise ServiceError("Usage: /selectmatch <match_id>")
    m = app.ms.get(int(args[0]))
    context.user_data["sel_match"] = m["id"]
    await reply(update, f"📌 Selected match #{m['id']} — <b>{e(m['name'])}</b>. "
                        "Admin commands without an ID will use it.")


@admin_command
async def admin_matches(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    rows = app.ms.list(limit=20)
    if not rows:
        await reply(update, "No matches yet. Use /creatematch.")
        return
    lines = ["📋 <b>All matches</b>"]
    for m in rows:
        g = f"group {m['group_id']}"
        lines.append(f"#{m['id']} · {e(m['name'])} · {m['mode']} · {STATUS_LABEL[m['status']]} · {g}")
    await reply(update, "\n".join(lines))


@admin_command
async def setmode(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    """/setmode [match_id] solo|team|custom [team_count team_size]"""
    args = list(context.args or [])
    mid = int(args.pop(0)) if args and args[0].isdigit() else None
    if not args or args[0].lower() not in MODES:
        raise ServiceError("Usage: /setmode [match_id] solo|team|custom [team_count team_size]")
    mode, extra = args[0].lower(), args[1:]
    if extra and not (len(extra) == 2 and all(x.isdigit() for x in extra)):
        raise ServiceError("Team settings must be two numbers: team_count team_size.")
    m = pick_admin_match(app, context, mid)
    changes = {"mode": mode}
    if extra:
        changes.update(team_count=int(extra[0]), team_size=int(extra[1]))
    new = app.ms.update_settings(update.effective_user.id, m["id"], **changes)
    await reply(update, f"✅ <b>{e(new['name'])}</b> (ID {new['id']}) is now <b>{fmt.mode_label(new)}</b>, "
                        f"capacity {new['capacity']}.")
    await refresh_announcements(context.bot, app, new["id"])


async def matches_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/matches: group members see announced matches; admins in private see all matches."""
    chat = update.effective_chat
    if chat is not None and chat.type == "private":
        await admin_matches(update, context)
    else:
        await matches(update, context)
