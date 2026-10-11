"""Slot views for everyone; /players and /setslots for admins."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from keyboards.player_menu import pager
from services import formatting as fmt
from services.errors import ServiceError
from services.permission_service import GROUP_TYPES
from .common import (App, admin_command, answer, e, get_app, group_command, pick_admin_match,
                     player_match, reply, show, split_match_arg)


@group_command
async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    text, page, pages = fmt.slots_page(app.rs.slots_view(m["id"]), 1)
    await reply(update, text, pager("slots", m["id"], page, pages))


@group_command
async def count(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    v = app.rs.slots_view(m["id"])
    await reply(update, f"📊 <b>{e(m['name'])}</b> (ID {m['id']})\nRegistered: {v['registered']}/{v['total']}\n"
                        f"Empty slots: {v['empty']}")


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pagination for /slots and /teams. Verified against the chat the button lives in."""
    q, chat = update.callback_query, update.effective_chat
    app = get_app(context)
    _, kind, mid, page = q.data.split(":")
    if kind == "noop" or chat is None or chat.type not in GROUP_TYPES or not app.perms.is_group_authorized(chat.id):
        await answer(update)
        return
    try:
        m = app.ms.get_for_group(int(mid), chat.id)
        if kind == "slots":
            text, p, pages = fmt.slots_page(app.rs.slots_view(m["id"]), int(page))
        else:
            text, p, pages = fmt.teams_page(app.ts.view(m["id"]), int(page))
    except (ServiceError, ValueError):
        await answer(update, "Not available.", True)
        return
    await answer(update)
    await show(update, text, pager(kind, m["id"], p, pages))


@admin_command
async def players(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    mid, _ = split_match_arg(context.args or [], 0)
    m = pick_admin_match(app, context, mid)
    regs = app.rs.slots_view(m["id"])["regs"]
    lines = [f"👥 <b>{e(m['name'])}</b> (ID {m['id']}) — {len(regs)} player(s)"]
    lines += [f"Slot {r['slot']} · {e(r['ign'])} · {r['uid']} · tg:{r['telegram_id']}" for r in regs]
    await reply(update, "\n".join(lines))


@admin_command
async def setslots(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    mid, rest = split_match_arg(context.args or [], 1)
    if not rest[0].isdigit():
        raise ServiceError("Usage: /setslots [match_id] <number>")
    m = pick_admin_match(app, context, mid)
    new = app.rs.set_capacity(update.effective_user.id, m["id"], int(rest[0]))
    v = app.rs.slots_view(m["id"])
    note = f"\n⚠️ {v['over_capacity']} registration(s) exceed the new capacity (kept)." if v["over_capacity"] else ""
    await reply(update, f"✅ <b>{e(new['name'])}</b> (ID {new['id']}): capacity is now {new['capacity']}.{note}")
