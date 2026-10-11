"""Handler tests with a mocked Telegram API (no network, no real tokens)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")

from handlers import admin, leaderboards, results, slots, teams  # noqa: E402
from services.errors import ServiceError  # noqa: E402
from tests.test_admin_flows import mk_bot  # noqa: E402
from tests.conftest import ADMIN, GROUP_A, GROUP_B, OWNER, PLAYER, register_n  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def ctx(tg, args=None, user_data=None, bot=None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"app": tg}), args=args or [],
                           user_data=user_data if user_data is not None else {}, bot=bot or mk_bot())


def upd(chat_type, chat_id, user_id, text="/x", callback_data=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=text)
    q = None
    if callback_data:
        q = SimpleNamespace(data=callback_data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type=chat_type, id=chat_id, title="T"),
        effective_user=SimpleNamespace(id=user_id, username="u"),
        effective_message=msg, callback_query=q)


def sent(u):
    return " ".join(str(c.args[0]) for c in u.effective_message.reply_text.call_args_list)


def test_admin_panel_rejected_in_group_even_for_owner(tg):
    u = upd("supergroup", GROUP_A, OWNER)
    run(admin.cmd_admin(u, ctx(tg)))
    kwargs = u.effective_message.reply_text.call_args.kwargs
    assert "private chat" in sent(u) and kwargs.get("reply_markup") is None


def test_admin_panel_rejected_for_unauthorised_private_user(tg):
    u = upd("private", PLAYER, PLAYER)
    run(admin.cmd_admin(u, ctx(tg)))
    assert "not authorized" in sent(u)
    assert u.effective_message.reply_text.call_args.kwargs.get("reply_markup") is None


def test_admin_panel_opens_for_admin_in_private(tg):
    u = upd("private", ADMIN, ADMIN)
    run(admin.cmd_admin(u, ctx(tg)))
    assert u.effective_message.reply_text.call_args.kwargs["reply_markup"] is not None


def test_admin_callbacks_checked_every_time(tg):
    # unauthorised user pressing a forged admin button in private
    u = upd("private", PLAYER, PLAYER, callback_data="adm:mm")
    run(admin.callback(u, ctx(tg)))
    u.callback_query.edit_message_text.assert_not_called()
    assert u.callback_query.answer.call_args.kwargs.get("show_alert") is True
    # admin button pressed inside a group
    g = upd("supergroup", GROUP_A, OWNER, callback_data="adm:mm")
    run(admin.callback(g, ctx(tg)))
    g.callback_query.edit_message_text.assert_not_called()
    # owner-only screen refused for a normal admin
    a = upd("private", ADMIN, ADMIN, callback_data="adm:ad")
    run(admin.callback(a, ctx(tg)))
    assert "owner" in a.callback_query.edit_message_text.call_args.args[0].lower()


def test_wizard_text_ignored_for_non_admins(tg):
    u = upd("private", PLAYER, PLAYER, text="hello")
    run(admin.flow_text(u, ctx(tg, user_data={"flow": {"kind": "search"}})))
    u.effective_message.reply_text.assert_not_called()


def test_unauthorised_group_is_refused(tg):
    u = upd("supergroup", -1003, PLAYER)
    run(slots.slots(u, ctx(tg)))
    assert "not authorized" in sent(u)


def test_slots_command_in_authorised_group(tg, make_match, rs):
    mid = make_match(capacity=3)
    register_n(rs, mid, 2)
    u = upd("supergroup", GROUP_A, PLAYER)
    run(slots.slots(u, ctx(tg)))
    text = sent(u)
    assert "Registered Players: 2" in text and "Slot 1 - Player1" in text and "Empty Slots: 1" in text


def test_player_in_any_authorized_group_sees_shared_match(tg, make_match):
    from tests.conftest import GROUP_B
    created_for_b = make_match(group=GROUP_B)
    u = upd("supergroup", GROUP_A, PLAYER)
    run(slots.slots(u, ctx(tg, args=[str(created_for_b)])))
    assert f"Match ID: {created_for_b}" in sent(u)
    u2 = upd("supergroup", GROUP_A, PLAYER)
    run(slots.slots(u2, ctx(tg, args=["9999"])))
    assert "not found" in sent(u2)


def test_jointeam_dynamic_command(tg, make_match, rs):
    mid = make_match(mode="team", teams=6, size=2)
    reg, = register_n(rs, mid, 1)
    u = upd("supergroup", GROUP_A, reg["telegram_id"], text="/jointeam6")
    run(teams.jointeam(u, ctx(tg)))
    assert "Team 6" in sent(u) and "1/2" in sent(u)
    assert teams.JOIN_RE.match("/jointeam12@FFregisterBot") and not teams.JOIN_RE.match("/jointeamx")


def test_points_shows_only_published_results(tg, make_match, rs, ms, res):
    mid = make_match()
    reg, = register_n(rs, mid, 1)
    ms.transition(OWNER, mid, "start")
    res.save(OWNER, mid, reg["uid"], 1, 5)
    u = upd("supergroup", GROUP_A, PLAYER)
    run(leaderboards.points(u, ctx(tg)))
    assert "No results have been published" in sent(u)
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=9)
    run(results.publish_results(bot, tg, OWNER, mid))
    assert {c.args[0] for c in bot.send_message.call_args_list} == {GROUP_A, GROUP_B}  # all authorized groups
    u2 = upd("supergroup", GROUP_A, PLAYER)
    run(leaderboards.points(u2, ctx(tg)))
    assert "Player1" in sent(u2) and "17" in sent(u2)


def test_publish_goes_only_to_match_group_and_handles_errors(tg, make_match, rs, ms, res):
    from telegram.error import Forbidden
    mid = make_match()
    reg, = register_n(rs, mid, 1)
    ms.transition(OWNER, mid, "start")
    with pytest.raises(ServiceError):
        run(results.publish_results(AsyncMock(), tg, OWNER, mid))  # nothing to publish
    res.save(OWNER, mid, reg["uid"], 1, 0)
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("bot was kicked")
    with pytest.raises(ServiceError):
        run(results.publish_results(bot, tg, OWNER, mid))
    assert res.published_snapshot(mid) is None  # nothing recorded when sending failed
