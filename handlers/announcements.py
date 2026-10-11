"""Match announcements and registration notices for ALL authorized groups.

Matches are shared: one match record, one announcement message per authorized group.
The announcement is edited in place when counts or status change. Nothing here may
break the caller: every Telegram failure is logged and skipped.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from telegram.error import BadRequest, Forbidden, TelegramError

from database import repositories as repo
from keyboards.player_menu import join_keyboard
from services import formatting as fmt
from .common import App, HTML, e

log = logging.getLogger(__name__)
_locks: Dict[int, asyncio.Lock] = {}


def authorized_group_ids(app: App) -> List[int]:
    with app.db.read() as conn:
        return [g["chat_id"] for g in repo.list_groups(conn) if g["authorized"]]


def _card(app: App, match_id: int):
    """Fresh text + keyboard straight from the database."""
    m = app.ms.get(match_id)
    registered = app.rs.slots_view(match_id)["registered"]
    open_now = m["status"] == "registration_open" and registered < m["capacity"]
    return m, fmt.announcement_text(m, registered), (join_keyboard(match_id) if open_now else None)


async def _send_card(bot, app: App, chat_id: int, match_id: int, text: str, markup) -> bool:
    try:
        sent = await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)
    except Forbidden:
        log.warning("Announce: no permission in chat %s (bot removed or blocked)", chat_id)
        return False
    except (BadRequest, TelegramError) as exc:
        log.warning("Announce failed in chat %s: %s", chat_id, exc)
        return False
    with app.db.transaction() as conn:
        repo.upsert_announcement(conn, match_id, chat_id, sent.message_id)
    return True


async def sync_announcements(bot, app: App, match_id: int, create_missing: bool) -> Dict[int, bool]:
    """Edit existing announcements (or post new ones). Returns {chat_id: success}.
    Serialised per match so a slow older edit can never overwrite a newer count."""
    lock = _locks.setdefault(match_id, asyncio.Lock())
    result: Dict[int, bool] = {}
    async with lock:
        m, text, markup = _card(app, match_id)  # read inside the lock = latest committed state
        for chat_id in authorized_group_ids(app):
            with app.db.read() as conn:
                ann = repo.get_announcement(conn, match_id, chat_id)
            if ann is None:
                if create_missing:
                    result[chat_id] = await _send_card(bot, app, chat_id, match_id, text, markup)
                continue
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=ann["message_id"],
                                            parse_mode=HTML, reply_markup=markup)
                result[chat_id] = True
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    result[chat_id] = True
                else:  # message deleted / too old to edit: post a fresh one and remember it
                    log.info("Edit failed in chat %s (%s); re-posting announcement", chat_id, exc)
                    result[chat_id] = await _send_card(bot, app, chat_id, match_id, text, markup)
            except Forbidden:
                log.warning("Refresh: no permission in chat %s", chat_id)
                result[chat_id] = False
            except TelegramError as exc:
                log.warning("Refresh failed in chat %s: %s", chat_id, exc)
                result[chat_id] = False
    return result


async def announce_new_match(bot, app: App, match_id: int) -> Dict[int, bool]:
    try:
        return await sync_announcements(bot, app, match_id, create_missing=True)
    except Exception:  # never break match creation
        log.exception("announce_new_match failed")
        return {}


async def refresh_announcements(bot, app: App, match_id: int) -> None:
    """Update counts/status on existing announcements; also posts into authorized groups that
    have none yet (only while registration is open)."""
    try:
        m = app.ms.get(match_id)
        await sync_announcements(bot, app, match_id, create_missing=m["status"] == "registration_open")
    except Exception:
        log.exception("refresh_announcements failed")


async def broadcast(bot, app: App, text: str) -> int:
    sent = 0
    for chat_id in authorized_group_ids(app):
        try:
            await bot.send_message(chat_id, text, parse_mode=HTML)
            sent += 1
        except Forbidden:
            log.warning("Broadcast: no permission in chat %s", chat_id)
        except TelegramError as exc:
            log.warning("Broadcast failed in chat %s: %s", chat_id, exc)
        except Exception:
            log.exception("Broadcast crashed for chat %s", chat_id)
    return sent


STATUS_NOTICE = {
    "registration_open": "🟢 Registration is now <b>open</b> for Match #{id} — {name}. Tap 🎮 Join Match or use <code>/joinmatch {id}</code>.",
    "registration_closed": "🔒 Registration is now <b>closed</b> for Match #{id} — {name}.",
    "in_progress": "🎮 Match #{id} — {name} has <b>started</b>.",
    "completed": "🏁 Match #{id} — {name} is <b>completed</b>.",
    "cancelled": "❌ Match #{id} — {name} was <b>cancelled</b>.",
}


async def after_status_change(bot, app: App, match) -> None:
    """Edit the announcements and post one short notice per group."""
    try:
        await refresh_announcements(bot, app, match["id"])
        note = STATUS_NOTICE.get(match["status"])
        if note:
            await broadcast(bot, app, note.format(id=match["id"], name=e(match["name"])))
    except Exception:
        log.exception("after_status_change failed")


async def notify_registration(bot, app: App, reg, match, team_name: Optional[str] = None) -> None:
    """Called ONLY after a successful registration (failures/duplicates never reach here)."""
    try:
        v = app.rs.slots_view(match["id"])
        text = (f"✅ <b>{e(reg['ign'])}</b> registered for Match #{match['id']} — {e(match['name'])}\n"
                + (f"👥 Team: {e(team_name)}\n" if team_name else "")
                + f"Registered: {v['registered']}/{v['total']} · Slots remaining: {v['remaining']}")
        await broadcast(bot, app, text)
        await refresh_announcements(bot, app, match["id"])
    except Exception:
        log.exception("notify_registration failed")


async def notify_team_join(bot, app: App, reg, match, team) -> None:
    try:
        view = app.ts.view(match["id"])
        cur = next(t for t in view["teams"] if t["team"]["id"] == team["id"])
        await broadcast(bot, app, f"👥 <b>{e(reg['ign'])}</b> joined <b>{e(team['name'])}</b> "
                                  f"({len(cur['members'])}/{match['team_size']}) in Match #{match['id']}")
    except Exception:
        log.exception("notify_team_join failed")
