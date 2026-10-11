"""/register conversation (runs in the group), /myregistration, /cancelregistration."""
from __future__ import annotations

from telegram import ForceReply, Update
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, filters)

from keyboards.player_menu import confirm_registration
from services import formatting as fmt
from services.errors import ServiceError
from services.match_service import OPEN
from services.registration_service import validate_ign, validate_uid
from .common import App, answer, e, get_app, group_command, player_match, reply, show

NAME, UID, CONFIRM = range(3)


async def _ask(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=ForceReply(selective=True, input_field_placeholder="Reply here"))


@group_command
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> int:
    chat, user = update.effective_chat, update.effective_user
    m = app.ms.active_for_group(chat.id)
    if m is None or m["status"] != OPEN:
        await reply(update, "⛔ Registration is not open right now. Check /activematch.")
        return ConversationHandler.END
    if app.rs.get_user_registration(m["id"], user.id):
        await reply(update, "✅ You are already registered. See /myregistration.")
        return ConversationHandler.END
    context.user_data["reg"] = {"match_id": m["id"]}
    await _ask(update, f"📝 <b>Registration — {e(m['name'])}</b>\n\n"
                       "Step 1/2: reply with your <b>Free Fire in-game name</b>.\n(Send /cancel to stop.)")
    return NAME


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
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    await reply(update, "✖️ Registration cancelled.")
    return ConversationHandler.END


def conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            NAME: [MessageHandler(text, got_name)],
            UID: [MessageHandler(text, got_uid)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^reg:(ok|redo|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv), CommandHandler("register", register_start)],
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
    await reply(update, f"🗑 Your registration (slot {reg['slot']}) was cancelled and the slot is free again.")
