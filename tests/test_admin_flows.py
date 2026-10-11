"""End-to-end admin wizard tests with mocked Telegram objects."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")

from handlers import admin  # noqa: E402
from tests.conftest import GROUP_A, OWNER, register_n  # noqa: E402


def run(c):
    return asyncio.run(c)


def mk_ctx(tg, user_data, bot=None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"app": tg}), args=[], user_data=user_data,
                           bot=bot or AsyncMock())


def msg_update(text):
    m = SimpleNamespace(reply_text=AsyncMock(), text=text)
    return SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=OWNER, title=""),
                           effective_user=SimpleNamespace(id=OWNER, username="o"), effective_message=m,
                           callback_query=None)


def cb_update(data):
    m = SimpleNamespace(reply_text=AsyncMock(), text="")
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=m)
    return SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=OWNER, title=""),
                           effective_user=SimpleNamespace(id=OWNER, username="o"), effective_message=m,
                           callback_query=q)


def last_text(u):
    if u.callback_query and u.callback_query.edit_message_text.call_args:
        return u.callback_query.edit_message_text.call_args.args[0]
    return u.effective_message.reply_text.call_args.args[0]


def press(tg, ud, data, bot=None):
    u = cb_update(data)
    run(admin.callback(u, mk_ctx(tg, ud, bot)))
    return u


def say(tg, ud, text):
    u = msg_update(text)
    run(admin.flow_text(u, mk_ctx(tg, ud)))
    return u


def test_create_team_match_wizard(tg, ms):
    ud = {}
    u = msg_update("/creatematch")
    run(admin.cmd_creatematch(u, mk_ctx(tg, ud)))  # two authorised groups -> asks which
    assert "Which group" in last_text(u)
    press(tg, ud, f"adm:cf:g:{GROUP_A}")
    say(tg, ud, "Sunday Squads")
    press(tg, ud, "adm:cf:m:team")
    say(tg, ud, "12")
    u = say(tg, ud, "4")
    assert "Start time" in last_text(u)
    say(tg, ud, "2030-02-01 21:00")
    press(tg, ud, "adm:cf:tz")
    u = press(tg, ud, "adm:cf:nodl")
    assert "Review new match" in last_text(u) and "48" in last_text(u)
    u = press(tg, ud, "adm:cf:go")
    m = ms.list(GROUP_A)[0]
    assert (m["name"], m["mode"], m["capacity"], m["team_count"], m["team_size"], m["status"]) == \
        ("Sunday Squads", "team", 48, 12, 4, "draft")
    assert ud["sel_match"] == m["id"] and "created" in last_text(u)


def test_create_wizard_rejects_bad_structure_then_recovers(tg, ms):
    ud = {}
    run(admin.cmd_creatematch(msg_update("/creatematch"), mk_ctx(tg, ud)))
    press(tg, ud, f"adm:cf:g:{GROUP_A}")
    say(tg, ud, "Custom Cup")
    press(tg, ud, "adm:cf:m:custom")
    say(tg, ud, "3")
    say(tg, ud, "2")
    u = say(tg, ud, "20")  # 20 > 3*2 -> refused, structure re-asked
    assert "larger than" in " ".join(str(c.args[0]) for c in u.effective_message.reply_text.call_args_list)
    assert "capacity" not in ud["flow"]["data"] and "team_count" not in ud["flow"]["data"]


def test_status_buttons_and_confirmations(tg, make_match, ms):
    mid = make_match(open_reg=False)
    ud = {}
    press(tg, ud, f"adm:ms:{mid}:open")
    assert ms.get(mid)["status"] == "registration_open"
    u = press(tg, ud, f"adm:c:endm:{mid}")
    assert "End match" in last_text(u)
    assert ms.get(mid)["status"] == "registration_open"  # nothing happened without confirmation
    press(tg, ud, f"adm:ms:{mid}:start")
    press(tg, ud, f"adm:y:endm:{mid}")
    assert ms.get(mid)["status"] == "completed"


def test_remove_player_needs_confirmation(tg, make_match, rs):
    mid = make_match()
    reg, = register_n(rs, mid, 1)
    ud = {}
    press(tg, ud, f"adm:fl:rmplayer:{mid}")
    u = say(tg, ud, reg["uid"])
    assert rs.slots_view(mid)["registered"] == 1  # still there: waiting for the Yes button
    press(tg, ud, f"adm:y:rmp:{mid}:{reg['id']}")
    assert rs.slots_view(mid)["registered"] == 0


def test_assign_slot_and_settings_wizards(tg, make_match, rs, ms):
    mid = make_match(capacity=10)
    reg, = register_n(rs, mid, 1)
    ud = {}
    press(tg, ud, f"adm:fl:assign:{mid}")
    say(tg, ud, reg["uid"])
    say(tg, ud, "7")
    assert rs.get_user_registration(mid, reg["telegram_id"])["slot"] == 7
    press(tg, ud, f"adm:fv:capacity:{mid}")
    say(tg, ud, "12")
    assert ms.get(mid)["capacity"] == 12
    press(tg, ud, f"adm:fv:placement:{mid}")
    say(tg, ud, "20,10,5")
    assert ms.rules(mid).placement == {1: 20, 2: 10, 3: 5}
    press(tg, ud, f"adm:tg:{mid}:cancel")
    assert ms.get(mid)["allow_cancel"] == 0


def test_result_wizard_and_publish(tg, make_match, rs, ms, res):
    mid = make_match(capacity=5)
    a, b = register_n(rs, mid, 2)
    ms.transition(OWNER, mid, "start")
    ud = {}
    press(tg, ud, f"adm:rstart:{mid}")
    press(tg, ud, f"adm:rsel:{mid}:{a['uid']}")
    say(tg, ud, "1")
    u = say(tg, ud, "8")
    assert "<b>20</b>" in " ".join(str(c.args[0]) for c in u.effective_message.reply_text.call_args_list)
    press(tg, ud, "adm:rsave")
    say(tg, ud, b["uid"])  # typing a UID also picks
    say(tg, ud, "2")
    say(tg, ud, "3")
    press(tg, ud, "adm:rsave")
    rows = res.live_snapshot(mid)["rows"]
    assert [(r["name"], r["total"]) for r in rows] == [("Player1", 20), ("Player2", 12)]
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=5)
    u = press(tg, ud, f"adm:y:pub:{mid}", bot)
    assert "published" in last_text(u).lower() and bot.send_message.call_args.args[0] == GROUP_A
    assert not res.has_unpublished_changes(mid)


def test_result_flow_requires_started_match(tg, make_match):
    mid = make_match()
    u = press(tg, {}, f"adm:rstart:{mid}")
    assert "Start the match first" in last_text(u)


def test_group_authorisation_owner_only(tg, db):
    u = press(tg, {}, "adm:gt:-1003")
    assert tg.perms.is_group_authorized(-1003)
    assert "✅ Unauthorized" in last_text(u)


def test_add_and_remove_admin_flow(tg):
    ud = {}
    press(tg, ud, "adm:fl:addadmin:0")
    say(tg, ud, "555")
    assert tg.perms.is_admin(555)
    press(tg, ud, "adm:fl:rmadmin:0")
    say(tg, ud, "555")
    assert tg.perms.is_admin(555)  # needs the Yes button
    press(tg, ud, "adm:y:rmadm:555")
    assert not tg.perms.is_admin(555)
