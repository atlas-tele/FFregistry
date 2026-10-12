"""Shared handler plumbing: app container, guards, helpers, /start /help /cancel."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from config import Config
from database import Database
from database import repositories as repo
from keyboards.player_menu import PLAYER_HELP
from services.errors import PermissionDenied, ServiceError
from services.formatting import split_message
from services.match_service import LIVE_STATUSES, MatchService, STATUS_LABEL
from services.permission_service import GROUP_TYPES, PermissionService
from services.registration_service import RegistrationService
from services.result_service import ResultService
from services.team_service import TeamService

log = logging.getLogger(__name__)
e = html.escape
HTML = ParseMode.HTML


@dataclass
class App:
    config: Config
    db: Database
    perms: PermissionService
    ms: MatchService
    rs: RegistrationService
    ts: TeamService
    res: ResultService


def build_app(config: Config, db: Database) -> App:
    return App(config, db, PermissionService(db, config.owner_id, config.admin_ids),
               MatchService(db), RegistrationService(db), TeamService(db), ResultService(db))


def get_app(context: ContextTypes.DEFAULT_TYPE) -> App:
    return context.application.bot_data["app"]


# ---- sending helpers ---------------------------------------------------
async def reply(update: Update, text: str, markup=None) -> None:
    """Reply with HTML, splitting long text. Markup is attached to the last chunk."""
    msg = update.effective_message
    if msg is None:
        return
    parts = split_message(text)
    for i, part in enumerate(parts):
        try:
            await msg.reply_text(part, parse_mode=HTML,
                                 reply_markup=markup if i == len(parts) - 1 else None)
        except Forbidden:
            log.warning("Forbidden while replying in chat %s", update.effective_chat and update.effective_chat.id)
            return
        except TelegramError:
            log.exception("Telegram error while replying")
            return


async def show(update: Update, text: str, markup=None) -> None:
    """Edit the callback message in place, or reply when there is none."""
    q = update.callback_query
    if q is None or q.message is None:
        await reply(update, text, markup)
        return
    try:
        await q.edit_message_text(text[:4000], parse_mode=HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        log.warning("edit failed (%s); sending a new message", exc)
        await reply(update, text, markup)
    except TelegramError:
        log.exception("Telegram error while editing")


async def answer(update: Update, text: str = "", alert: bool = False) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer(text[:190], show_alert=alert)
        except TelegramError:
            pass


# ---- guards ------------------------------------------------------------
def group_command(func):
    """Player commands: authorised group chats only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None or update.effective_message is None:
            return
        app = get_app(context)
        if chat.type == "private":
            await reply(update, "ℹ️ Please use this command in the tournament group.")
            return
        if chat.type not in GROUP_TYPES:
            return
        with app.db.transaction() as conn:
            if repo.get_group(conn, chat.id) is None:
                repo.upsert_group(conn, chat.id, chat.title or "")
        if not app.perms.is_group_authorized(chat.id):
            await reply(update, "🚫 This group is not authorized yet. Ask the bot owner to authorize it.")
            return
        try:
            return await func(update, context, app, *a, **kw)
        except ServiceError as exc:
            await reply(update, f"⚠️ {e(exc.message)}")
    return wrapper


def group_callback(func):
    """Inline-button handlers used by players: authorised group chats only; errors shown as popups."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat, user, q = update.effective_chat, update.effective_user, update.callback_query
        if chat is None or user is None or q is None:
            return
        app = get_app(context)
        if chat.type not in GROUP_TYPES:
            await answer(update, "Please use this button in the tournament group.", True)
            return
        with app.db.transaction() as conn:
            if repo.get_group(conn, chat.id) is None:
                repo.upsert_group(conn, chat.id, chat.title or "")
        if not app.perms.is_group_authorized(chat.id):
            await answer(update, "This group is not authorized yet.", True)
            return
        try:
            return await func(update, context, app, *a, **kw)
        except ServiceError as exc:
            await answer(update, exc.message, True)
    return wrapper


def admin_command(func):
    """Admin commands: private chat + authorised numeric user ID, checked on every call."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None or update.effective_message is None:
            return
        app = get_app(context)
        try:
            app.perms.require_admin_context(chat.type, user.id)
        except PermissionDenied as exc:
            await reply(update, "🔒 " + e(exc.message))
            return
        try:
            return await func(update, context, app, *a, **kw)
        except ServiceError as exc:
            await reply(update, f"⚠️ {e(exc.message)}")
    return wrapper


def admin_callback(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat, user, q = update.effective_chat, update.effective_user, update.callback_query
        if chat is None or user is None or q is None:
            return
        app = get_app(context)
        try:
            app.perms.require_admin_context(chat.type, user.id)
        except PermissionDenied as exc:
            await answer(update, exc.message, alert=True)
            return
        try:
            return await func(update, context, app, *a, **kw)
        except ServiceError as exc:
            await answer(update)
            await show(update, f"⚠️ {e(exc.message)}", _back_markup())
    return wrapper


def _back_markup():
    from keyboards.admin_panel import with_back
    return with_back([])


# ---- argument helpers --------------------------------------------------
def split_match_arg(args: list, n_extra: int):
    """Return (match_id or None, remaining_args). A leading numeric arg is the match ID."""
    if len(args) == n_extra + 1 and args[0].isdigit():
        return int(args[0]), args[1:]
    if len(args) == n_extra:
        return None, args
    raise ServiceError("Wrong number of arguments. Check /help for the syntax.", "usage")


def pick_admin_match(app: App, context, match_id: Optional[int]):
    """Explicit ID > selected match > the only live match. Anything else is refused as ambiguous."""
    if match_id is not None:
        return app.ms.get(match_id)
    sel = context.user_data.get("sel_match")
    if sel:
        try:
            return app.ms.get(sel)
        except ServiceError:
            context.user_data.pop("sel_match", None)
    live = app.ms.list(statuses=LIVE_STATUSES, limit=10)
    if len(live) == 1:
        return live[0]
    if not live:
        raise ServiceError("There are no active matches. Create one with /creatematch.")
    lines = "; ".join(f"#{m['id']} {m['name']} ({STATUS_LABEL[m['status']]})" for m in live[:8])
    raise ServiceError(f"Several matches are active, so I won't guess. Add the match ID or use /selectmatch <id>. {lines}")


def player_match(app: App, update: Update, args: list):
    """Group match for player commands: explicit ID (same group only) or the group's active match."""
    chat_id = update.effective_chat.id
    if args and args[0].isdigit():
        return app.ms.get_for_group(int(args[0]), chat_id)
    m = app.ms.active_for_group(chat_id)
    if m is None:
        raise ServiceError("There is no active match in this group right now. Check /matches.")
    return m


# ---- basic commands ----------------------------------------------------
def remember_group(app: App, chat) -> None:
    """Record any group the bot is used in (unauthorized until the owner approves it)."""
    if chat is not None and chat.type in GROUP_TYPES:
        with app.db.transaction() as conn:
            if repo.get_group(conn, chat.id) is None:
                repo.upsert_group(conn, chat.id, chat.title or "")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return
    app = get_app(context)
    remember_group(app, chat)
    if chat.type == "private":
        extra = "\n\n🔧 You are an admin — send /admin to open the panel." if app.perms.is_admin(user.id) else ""
        await reply(update, "👋 <b>Welcome to FFregister!</b>\n\nI manage Free Fire custom-room registration, "
                            "slots, teams and results. Players register in the tournament group with /register." + extra)
    else:
        await reply(update, "✅ Yes, I am online and ready! 🔥\nSend /help to see what I can do.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    app = get_app(context)
    remember_group(app, chat)
    team_cmds = ""
    if chat and chat.type in GROUP_TYPES:
        m = app.ms.active_for_group(chat.id)
        if m and m["team_count"]:
            n = m["team_count"]
            team_cmds = "/jointeam1" + (f" … /jointeam{n} — join a team\n" if n > 1 else " — join the team\n")
    text = PLAYER_HELP.replace("{team_cmds}", team_cmds)
    if chat and chat.type == "private" and update.effective_user and app.perms.is_admin(update.effective_user.id):
        text += ("\n<b>Admin (private chat only)</b>\n/admin · /creatematch · /matches · /selectmatch &lt;id&gt;\n"
                 "/open /close /startmatch /endmatch /cancelmatch [id]\n"
                 "/setmode [id] solo|team|custom [teams size] · /setslots [id] &lt;n&gt;\n"
                 "/players [id] · /submitresult [id] · /editresult &lt;id&gt; &lt;UID|T#&gt; &lt;place&gt; &lt;kills&gt;\n"
                 "/deleteresult &lt;id&gt; &lt;UID|T#&gt; · /publishresult &lt;id&gt; · /playerkills &lt;id&gt; &lt;UID&gt; &lt;kills&gt;\n")
    await reply(update, text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Outside a conversation: clears an admin flow, otherwise explains."""
    if context.user_data.pop("flow", None):
        await reply(update, "✖️ Cancelled.")
    else:
        await reply(update, "Nothing to cancel. To withdraw a registration use /cancelregistration.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again in a moment.")
        except TelegramError:
            pass
            
