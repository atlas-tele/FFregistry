"""FFregister — Free Fire custom room manager bot. Single polling process."""
from __future__ import annotations

import logging
import sys
import warnings

from telegram import BotCommand, Update
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from config import Config, ConfigError, load_config
from database import Database, repositories as repo, run_migrations
from handlers import admin, leaderboards, matches, registration, results, slots, teams
from handlers.common import build_app, cmd_cancel, cmd_help, cmd_start, on_error

# Informational PTB notice about ConversationHandler(per_message=False) + CallbackQueryHandler.
# Our conversation is tracked per chat+user on purpose, so the warning does not apply.
warnings.filterwarnings("ignore", message=".*per_message=False.*CallbackQueryHandler.*")

log = logging.getLogger("ffregister")


class RedactTokenFilter(logging.Filter):
    """Never let the bot token reach the logs (HTTP client URLs contain it)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token:
            record.msg = str(record.getMessage()).replace(self.token, "***")
            record.args = ()
        return True


def setup_logging(cfg: Config) -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=getattr(logging, cfg.log_level, logging.INFO), stream=sys.stdout)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactTokenFilter(cfg.bot_token))
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


PUBLIC_COMMANDS = [
    BotCommand("register", "Register for the active match"),
    BotCommand("joinmatch", "Join a match: /joinmatch 13"),
    BotCommand("myregistration", "Show my slot and details"),
    BotCommand("slots", "Show slots"),
    BotCommand("count", "Registered player count"),
    BotCommand("teams", "Show teams"),
    BotCommand("myteam", "Show my team"),
    BotCommand("leaveteam", "Leave my team"),
    BotCommand("matches", "List matches"),
    BotCommand("activematch", "Show the active match"),
    BotCommand("resultmatch", "Published results: /resultmatch 1"),
    BotCommand("points", "Leaderboards"),
    BotCommand("help", "Help"),
    BotCommand("cancel", "Cancel the current step"),
]


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(PUBLIC_COMMANDS)
    me = await application.bot.get_me()
    log.info("Bot started as @%s", me.username)


def register_handlers(app: Application) -> None:
    app.add_handler(registration.conversation())
    cmds = {
        "start": cmd_start, "help": cmd_help, "cancel": cmd_cancel,
        "myregistration": registration.my_registration, "cancelregistration": registration.cancel_registration,
        "slots": slots.slots, "count": slots.count, "players": slots.players, "setslots": slots.setslots,
        "matches": matches.matches_entry, "activematch": matches.activematch,
        "open": matches.open_reg, "close": matches.close_reg, "startmatch": matches.start_match,
        "endmatch": matches.end_match, "cancelmatch": matches.cancel_match,
        "selectmatch": matches.select_match, "setmode": matches.setmode,
        "admin": admin.cmd_admin, "creatematch": admin.cmd_creatematch,
        "submitresult": results.submit_result, "editresult": results.edit_result,
        "deleteresult": results.delete_result, "publishresult": results.publish_result,
        "playerkills": results.player_kills, "resultmatch": results.result_match,
        "points": leaderboards.points,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    for h in teams.handlers():
        app.add_handler(h)
    app.add_handler(CallbackQueryHandler(admin.callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(slots.page_callback, pattern=r"^pg:"))
    app.add_handler(ChatMemberHandler(admin.track_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin.flow_text))
    app.add_error_handler(on_error)


def build_application(cfg: Config, db: Database) -> Application:
    application = Application.builder().token(cfg.bot_token).post_init(post_init).build()
    application.bot_data["app"] = build_app(cfg, db)
    register_handlers(application)
    return application


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    setup_logging(cfg)
    db = Database(cfg.database_path)
    version = run_migrations(db)
    log.info("Database ready at %s (schema v%s)", cfg.database_path, version)
    with db.transaction() as conn:
        for gid in cfg.group_ids:
            repo.upsert_group(conn, gid, "", True)
    build_application(cfg, db).run_polling(
        allowed_updates=["message", "callback_query", "my_chat_member"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
  
