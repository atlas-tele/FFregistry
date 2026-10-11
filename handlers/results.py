"""Result commands (admin, private chat) and publishing."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from keyboards.admin_panel import confirm
from services import formatting as fmt
from services.errors import ServiceError
from services.permission_service import GROUP_TYPES
from .common import (App, HTML, admin_command, e, get_app, pick_admin_match, reply, split_match_arg)
from .announcements import authorized_group_ids
from .leaderboards import render

log = logging.getLogger(__name__)


def live_text(app: App, m) -> str:
    """Admin preview of the live leaderboard, clearly flagged when not published."""
    snap = app.res.live_snapshot(m["id"])
    pub = app.res.published_snapshot(m["id"])
    if pub is None:
        note = "🟡 <b>UNPUBLISHED</b> — only admins can see this"
    elif app.res.has_unpublished_changes(m["id"]):
        note = "🟠 <b>PUBLISHED, BUT EDITED SINCE</b> — players still see the old version until you republish"
    else:
        note = "🟢 Published — matches what players see"
    return fmt.leaderboard(f"{m['name']} — Match #{m['id']}", snap, snap["rows"], status_note=note)


async def publish_results(bot, app: App, admin_id: int, match_id: int) -> str:
    """Send the leaderboard to EVERY authorized group (matches are shared), then record the snapshot."""
    m = app.ms.get(match_id)
    groups = authorized_group_ids(app)
    if not groups:
        raise ServiceError("No authorized groups to publish to.", "group")
    snap = app.res.live_snapshot(match_id)
    if not snap["rows"]:
        raise ServiceError("There are no results to publish yet.")
    title = f"{m['name']} — Match #{m['id']}"
    text = "📢 <b>OFFICIAL RESULTS</b>\n" + fmt.leaderboard(title, snap, snap["rows"])
    first_id, first_chat, failed = None, None, 0
    for gid in groups:
        try:
            for part in fmt.split_message(text):
                sent = await bot.send_message(gid, part, parse_mode=HTML)
                if first_id is None:
                    first_id, first_chat = sent.message_id, gid
        except Forbidden:
            log.warning("Publish: no permission in chat %s", gid)
            failed += 1
        except BadRequest as exc:
            log.warning("Publish BadRequest in %s: %s", gid, exc)
            failed += 1
        except TelegramError:
            log.exception("Publish failed in %s", gid)
            failed += 1
    if first_id is None:
        raise ServiceError("I couldn't post in any authorized group (removed, blocked, or no permission).")
    app.res.record_publish(admin_id, match_id, first_chat, first_id, snap)
    warn = f"\n⚠️ Could not post in {failed} group(s)." if failed else ""
    return f"✅ Results for <b>{e(m['name'])}</b> were published to {len(groups) - failed} group(s).{warn}"


@admin_command
async def submit_result(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    from .admin import start_result_flow  # local import: admin.py imports this module
    mid, _ = split_match_arg(context.args or [], 0)
    m = pick_admin_match(app, context, mid)
    await start_result_flow(update, context, app, m["id"])


@admin_command
async def edit_result(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    args = context.args or []
    if len(args) != 4 or not (args[0].isdigit() and args[2].isdigit() and args[3].isdigit()):
        raise ServiceError("Usage: /editresult <match_id> <UID|T#> <placement> <kills>\nExample: /editresult 1 123456789 1 8")
    label, score = app.res.save(update.effective_user.id, int(args[0]), args[1], int(args[2]), int(args[3]),
                                overwrite=True)
    await reply(update, f"✏️ Updated <b>{e(label)}</b>: placement {args[2]}, {args[3]} kills → "
                        f"<b>{score.total}</b> pts.\n🟠 Remember to /publishresult {args[0]} again.")


@admin_command
async def delete_result(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    args = context.args or []
    if len(args) != 2 or not args[0].isdigit():
        raise ServiceError("Usage: /deleteresult <match_id> <UID|T#>")
    await reply(update, f"⚠️ Delete the result of <b>{e(args[1])}</b> in match #{args[0]}?",
                confirm(f"adm:y:delres:{args[0]}:{args[1]}"))


@admin_command
async def publish_result(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        raise ServiceError("Usage: /publishresult <match_id>")
    m = app.ms.get(int(args[0]))
    await reply(update, live_text(app, m) + f"\n\nPublish to group {m['group_id']}?",
                confirm(f"adm:y:pub:{m['id']}", yes_label="📢 Publish"))


@admin_command
async def player_kills(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    args = context.args or []
    if len(args) != 3 or not (args[0].isdigit() and args[2].isdigit()):
        raise ServiceError("Usage: /playerkills <match_id> <UID> <kills>  (team matches; informational)")
    name = app.res.save_player_kills(update.effective_user.id, int(args[0]), args[1], int(args[2]))
    await reply(update, f"🎯 Saved {args[2]} kill(s) for {e(name)}. These are shown separately and never added to team points.")


async def result_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resultmatch <id>: players see published results; admins in private see the live draft."""
    app = get_app(context)
    chat, user = update.effective_chat, update.effective_user
    args = context.args or []
    if chat is None or user is None:
        return
    if not (len(args) == 1 and args[0].isdigit()):
        await reply(update, "Usage: /resultmatch <match_id>")
        return
    if chat.type == "private":
        if not app.perms.is_admin(user.id):
            await reply(update, "ℹ️ Use this command in the tournament group.")
            return
        try:
            await reply(update, live_text(app, app.ms.get(int(args[0]))))
        except ServiceError as exc:
            await reply(update, f"⚠️ {e(exc.message)}")
        return
    if chat.type not in GROUP_TYPES or not app.perms.is_group_authorized(chat.id):
        return
    try:
        m = app.ms.get_for_group(int(args[0]), chat.id)
    except ServiceError as exc:
        await reply(update, f"⚠️ {e(exc.message)}")
        return
    snap = app.res.published_snapshot(m["id"])
    await reply(update, render(app, m, snap) if snap else f"Results for match #{m['id']} have not been published yet.")
