"""/register, /joinmatch <id> and the "🎮 Join Match" button (all share ONE conversation and the
same RegistrationService), plus /myregistration and /cancelregistration."""
from __future__ import annotations

from telegram import ForceReply, Update
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, filters)

from keyboards.player_menu import confirm_registration
from services import formatting as fmt
from services.errors import ServiceError
from services.match_service import OPEN
from services.registration_service import validate_ign, validate_uid
from .announcements import notify_registration, refresh_announcements
from .common import (App, answer, e, get_app, group_callback, group_command, player_match, reply, show)

NAME, UID, CONFIRM = range(3)


async def _ask(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=ForceReply(selective=True, input_field_placeholder="Reply here"))


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App, m) -> int:
    """Shared by /register, /joinmatch and the Join button. Raises ServiceError with a clear reason."""
    user = update.effective_user
    if m["status"] != OPEN:
        raise ServiceError(f"Registration is closed for Match #{m['id']} ({m['name']}).", "closed")
    if app.rs.slots_view(m["id"])["remaining"] <= 0:
        raise ServiceError(f"Match #{m['id']} is full. 😔 No slots remaining.", "full")
    old = app.rs.get_user_registration(m["id"], user.id)
    if old:  # same Telegram account, any group: one registration per match
        raise ServiceError(f"You are already registered for Match #{m['id']} (slot {old['slot']}).", "duplicate")
    context.user_data["reg"] = {"match_id": m["id"]}
    profile = app.rs.latest_profile(user.id)
    if profile:  # returning player: skip the questions, just confirm
        d = context.user_data["reg"]
        d["ign"], d["uid"] = profile
        await reply(update, f"🎮 <b>Join {e(m['name'])}</b> (Match #{m['id']})\n\n"
                            f"👤 Name: {e(d['ign'])}\n🆔 UID: {d['uid']}\n\nUse these details?",
                    confirm_registration())
        return CONFIRM
    await _ask(update, f"📝 <b>Registration — {e(m['name'])}</b> (Match #{m['id']})\n\n"
                       "Step 1/2: reply with your <b>Free Fire in-game name</b>.\n(Send /cancel to stop.)")
    return NAME


@group_command
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> int:
    open_now = app.ms.list(None, [OPEN], 10)
    if len(open_now) > 1:
        names = "\n".join(f"#{m['id']} {m['name']} → /joinmatch {m['id']}" for m in open_now[:8])
        raise ServiceError("Several matches are open. Pick one:\n" + names)
    m = app.ms.active_for_group(update.effective_chat.id)
    if m is None:
        raise ServiceError("Registration is not open right now. Check /activematch.")
    return await _begin(update, context, app, m)


@group_command
async def joinmatch(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> int:
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit():
        raise ServiceError("Usage: /joinmatch <match_id>   e.g. /joinmatch 13")
    return await _begin(update, context, app, app.ms.get(int(args[0])))


@group_callback
async def join_button(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> int:
    await answer(update)
    m = app.ms.get(int(update.callback_query.data.split(":")[1]))
    try:
        return await _begin(update, context, app, m)
    except ServiceError as exc:  # popup instead of chat spam
        await answer(update, exc.message.replace("<", ""), True)
        return ConversationHandler.END


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["reg"]["ign"] = validate_ign(update.effective_message.text)
    except ServiceError as exc:
        await _ask(update, f"⚠️ {e(exc.message)}\nPlease reply with your in-game name again.")
        return NAME
    except KeyError:
        return ConversationHandler.END
    await _ask(update, "Step 2/2: reply with your <b>Free Fire UID</b> (numbers only).")
    return UID


async def got_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["reg"]["uid"] = validate_uid(update.effective_message.text)
    except ServiceError as exc:
        await _ask(update, f"⚠️ {e(exc.message)}\nPlease reply with your UID again.")
        return UID
    except KeyError:
        return ConversationHandler.END
    d = context.user_data["reg"]
    await reply(update, "🔎 <b>Please confirm your details</b>\n\n"
                        f"👤 Name: {e(d['ign'])}\n🆔 UID: {d['uid']}", confirm_registration())
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    app = get_app(context)
    d = context.user_data.get("reg")
    if not d or "uid" not in d:
        await answer(update, "This registration expired. Send /register again.", True)
        return ConversationHandler.END
    action = q.data.split(":")[1]
    if action == "redo":
        await answer(update)
        await show(update, "✏️ Starting over…")
        await _ask(update, "Step 1/2: reply with your <b>Free Fire in-game name</b>.")
        return NAME
    if action == "no":
        context.user_data.pop("reg", None)
        await answer(update)
        await show(update, "❌ Registration cancelled.")
        return ConversationHandler.END
    try:
        reg, m = app.rs.register(d["match_id"], update.effective_user.id, update.effective_user.username,
                                 d["ign"], d["uid"])
    except ServiceError as exc:
        await answer(update)
        await show(update, f"⚠️ {e(exc.message)}")
        context.user_data.pop("reg", None)
        return ConversationHandler.END
    context.user_data.pop("reg", None)
    await answer(update, "Registered!")
    await show(update, fmt.registration_success(reg, m))
    await notify_registration(context.bot, app, reg, m)  # announce + refresh counts in ALL groups
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    await reply(update, "✖️ Registration cancelled.")
    return ConversationHandler.END


def conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_start), CommandHandler("joinmatch", joinmatch),
                      CallbackQueryHandler(join_button, pattern=r"^join:\d+$")],
        states={
            NAME: [MessageHandler(text, got_name)],
            UID: [MessageHandler(text, got_uid)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^reg:(ok|redo|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv), CommandHandler("register", register_start),
                   CommandHandler("joinmatch", joinmatch), CallbackQueryHandler(join_button, pattern=r"^join:\d+$")],
        allow_reentry=True,
        name="register",
    )


@group_command
async def my_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    reg = app.rs.get_user_registration(m["id"], update.effective_user.id)
    if not reg:
        await reply(update, f"You are not registered for <b>{e(m['name'])}</b>. Use /register.")
        return
    _, team = app.ts.team_of(m["id"], update.effective_user.id)
    text = fmt.my_registration(reg, m)
    if team:
        text += f"\n👥 Team: {e(team['team']['name'])}"
    await reply(update, text)


@group_command
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    m = player_match(app, update, context.args or [])
    reg = app.rs.cancel_own(m["id"], update.effective_user.id)
    await refresh_announcements(context.bot, app, m["id"])
    await reply(update, f"🗑 Your registration (slot {reg['slot']}) was cancelled and the slot is free again.")
