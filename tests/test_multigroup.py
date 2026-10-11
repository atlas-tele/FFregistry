"""Custom capacity, /joinmatch + Join button, shared multi-group matches, announcements."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")

from telegram.error import BadRequest, Forbidden  # noqa: E402

from database import Database, repositories as repo, run_migrations  # noqa: E402
from database.migrations import MIGRATIONS  # noqa: E402
from handlers import admin, announcements, registration, slots  # noqa: E402
from services.errors import ServiceError  # noqa: E402
from services.formatting import announcement_text, mode_label  # noqa: E402
from tests.conftest import GROUP_A, GROUP_B, OWNER, register_n  # noqa: E402
from tests.test_admin_flows import cb_update, last_text, msg_update, press, say  # noqa: E402


def run(c):
    return asyncio.run(c)


def mk_bot(fail_chats=(), edit_error=None):
    bot = AsyncMock()
    counter = iter(range(500, 100000))

    async def _send(chat_id, *a, **k):
        if chat_id in fail_chats:
            raise Forbidden("bot was blocked")
        return SimpleNamespace(message_id=next(counter))
    bot.send_message.side_effect = _send
    if edit_error:
        bot.edit_message_text.side_effect = edit_error
    return bot


def mk_ctx(tg, user_data=None, bot=None, args=None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"app": tg}), args=args or [],
                           user_data=user_data if user_data is not None else {}, bot=bot or mk_bot())


def group_msg(chat_id, user_id, text="/x"):
    m = SimpleNamespace(reply_text=AsyncMock(), text=text)
    return SimpleNamespace(effective_chat=SimpleNamespace(type="supergroup", id=chat_id, title="G"),
                           effective_user=SimpleNamespace(id=user_id, username="u"),
                           effective_message=m, callback_query=None)


def group_cb(chat_id, user_id, data):
    u = group_msg(chat_id, user_id)
    u.callback_query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=u.effective_message)
    return u


def popup(u):
    c = u.callback_query.answer.call_args_list
    return " ".join(str(x.args[0]) if x.args else "" for x in c)


def replies(u):
    return " ".join(str(c.args[0]) for c in u.effective_message.reply_text.call_args_list)


def join_via_command(tg, chat_id, user_id, match_id, ign, uid, bot=None, ud=None):
    """Drive the real conversation callbacks: /joinmatch -> name -> UID -> confirm."""
    ud = ud if ud is not None else {}
    bot = bot or mk_bot()
    state = run(registration.joinmatch(group_msg(chat_id, user_id), mk_ctx(tg, ud, bot, [str(match_id)])))
    if state == registration.NAME:
        assert run(registration.got_name(group_msg(chat_id, user_id, ign), mk_ctx(tg, ud, bot))) == registration.UID
        assert run(registration.got_uid(group_msg(chat_id, user_id, uid), mk_ctx(tg, ud, bot))) == registration.CONFIRM
    return run(registration.confirm(group_cb(chat_id, user_id, "reg:ok"), mk_ctx(tg, ud, bot))), bot


# ---------------- capacity ----------------
def test_capacity_is_per_match_and_validated(ms, rs, make_match):
    small = make_match(capacity=2, name="Small")
    big = make_match(capacity=48, name="Big", open_reg=False)
    assert (ms.get(small)["capacity"], ms.get(big)["capacity"]) == (2, 48)
    for bad in (0, -5, 101):
        with pytest.raises(ServiceError):
            ms.create_match(OWNER, GROUP_A, "Bad", "solo", "2030-01-01 21:00", "Asia/Kolkata", bad)
    register_n(rs, small, 2)
    with pytest.raises(ServiceError) as ei:
        rs.register(small, 77, "z", "Extra", "777777777")
    assert ei.value.code == "full"
    assert rs.slots_view(big)["remaining"] == 48  # untouched by the other match


def test_remaining_never_negative(ms, rs, make_match):
    mid = make_match(capacity=10)
    register_n(rs, mid, 6)
    ms.update_settings(OWNER, mid, capacity=4)
    assert rs.slots_view(mid)["remaining"] == 0
    assert "Slots remaining: 0" in announcement_text(ms.get(mid), 6)
    assert "Full" in announcement_text(ms.get(mid), 6)


def test_duo_and_squad_capacity_must_fit_whole_teams(ms, rs, make_match):
    squad = make_match(mode="team", teams=12, size=4, name="Squad Night")
    assert mode_label(ms.get(squad)) == "Squad" and ms.get(squad)["capacity"] == 48
    duo = make_match(mode="team", teams=5, size=2, name="Duo Night", open_reg=False)
    assert mode_label(ms.get(duo)) == "Duo"
    rs.set_capacity(OWNER, squad, 40)
    assert (ms.get(squad)["team_count"], ms.get(squad)["capacity"]) == (10, 40)
    with pytest.raises(ServiceError):
        rs.set_capacity(OWNER, squad, 42)
    assert mode_label(ms.get(make_match(name="Solo Night", open_reg=False))) == "Solo"


def test_wizard_duo_and_squad_presets(tg, ms):
    ud, bot = {}, mk_bot()
    run(admin.cmd_creatematch(msg_update("/creatematch"), SimpleNamespace(
        application=SimpleNamespace(bot_data={"app": tg}), args=[], user_data=ud, bot=bot)))
    say(tg, ud, "Squad Night")
    press(tg, ud, "adm:cf:m:squad")
    u = say(tg, ud, "50")  # not a multiple of 4 -> refused and re-asked
    assert "multiple of 4" in " ".join(str(c.args[0]) for c in u.effective_message.reply_text.call_args_list)
    say(tg, ud, "48")
    say(tg, ud, "2030-03-01 21:00")
    press(tg, ud, "adm:cf:tz")
    u = press(tg, ud, "adm:cf:nodl")
    assert "squad" in last_text(u)
    press(tg, ud, "adm:cf:go:open", bot)
    m = ms.list()[0]
    assert (m["mode"], m["team_count"], m["team_size"], m["capacity"], m["status"]) == ("team", 12, 4, 48, "registration_open")


# ---------------- both join methods, many groups ----------------
def test_joinmatch_command_registers_and_announces_everywhere(tg, make_match, rs):
    mid = make_match(capacity=3, name="Friday Night Custom")
    state, bot = join_via_command(tg, GROUP_A, 1, mid, "PlayerAlpha", "123456789")
    assert state == registration.ConversationHandler.END
    reg = rs.get_user_registration(mid, 1)
    assert reg["slot"] == 1 and reg["ign"] == "PlayerAlpha"
    notices = [c for c in bot.send_message.call_args_list if "PlayerAlpha" in c.args[1]]
    assert sorted(c.args[0] for c in notices) == sorted([GROUP_A, GROUP_B])  # one notice per authorized group
    text = notices[0].args[1]
    assert "Registered: 1/3" in text and "Slots remaining: 2" in text
    assert all(c.args[0] != -1003 for c in bot.send_message.call_args_list)  # never the unauthorized group


def test_join_button_uses_same_logic_and_shared_slots(tg, make_match, rs):
    mid = make_match(capacity=3)
    # player 1 via command in group A
    join_via_command(tg, GROUP_A, 1, mid, "Alpha", "111111111")
    # player 2 via the button in group B
    ud, bot = {}, mk_bot()
    state = run(registration.join_button(group_cb(GROUP_B, 2, f"join:{mid}"), mk_ctx(tg, ud, bot)))
    assert state == registration.NAME
    run(registration.got_name(group_msg(GROUP_B, 2, "Beta"), mk_ctx(tg, ud, bot)))
    run(registration.got_uid(group_msg(GROUP_B, 2, "222222222"), mk_ctx(tg, ud, bot)))
    run(registration.confirm(group_cb(GROUP_B, 2, "reg:ok"), mk_ctx(tg, ud, bot)))
    v = rs.slots_view(mid)
    assert [r["slot"] for r in v["regs"]] == [1, 2] and v["remaining"] == 1  # one shared slot list


def test_no_double_registration_across_groups_and_no_announcement(tg, make_match, rs):
    mid = make_match(capacity=5)
    join_via_command(tg, GROUP_A, 1, mid, "Alpha", "111111111")
    bot = mk_bot()
    u = group_cb(GROUP_B, 1, f"join:{mid}")
    run(registration.join_button(u, mk_ctx(tg, {}, bot)))  # same Telegram account, other group
    assert "already registered" in popup(u)
    assert rs.slots_view(mid)["registered"] == 1
    bot.send_message.assert_not_called()  # failed attempt = no announcement
    u2 = group_msg(GROUP_B, 1)
    run(registration.joinmatch(u2, mk_ctx(tg, {}, bot, [str(mid)])))
    assert "already registered" in replies(u2)
    bot.send_message.assert_not_called()


def test_full_closed_and_invalid_match_messages(tg, make_match, rs, ms):
    full = make_match(capacity=1)
    register_n(rs, full, 1)
    u = group_cb(GROUP_A, 9, f"join:{full}")
    run(registration.join_button(u, mk_ctx(tg)))
    assert "full" in popup(u).lower()
    closed = make_match(open_reg=False, name="Closed one")
    u = group_cb(GROUP_A, 9, f"join:{closed}")
    run(registration.join_button(u, mk_ctx(tg)))
    assert "closed" in popup(u).lower()
    u = group_msg(GROUP_A, 9)
    run(registration.joinmatch(u, mk_ctx(tg, args=["9999"])))
    assert "not found" in replies(u)
    u = group_msg(GROUP_A, 9)
    run(registration.joinmatch(u, mk_ctx(tg, args=["abc"])))
    assert "Usage: /joinmatch" in replies(u)


def test_unauthorised_group_cannot_join(tg, make_match):
    mid = make_match()
    u = group_cb(-1003, 5, f"join:{mid}")
    run(registration.join_button(u, mk_ctx(tg)))
    assert "not authorized" in popup(u)
    u = group_msg(-1003, 5)
    run(registration.joinmatch(u, mk_ctx(tg, args=[str(mid)])))
    assert "not authorized" in replies(u)


def test_returning_player_confirms_saved_details(tg, make_match, rs, ms):
    m1 = make_match(name="One")
    join_via_command(tg, GROUP_A, 1, m1, "Alpha", "111111111")
    ms.transition(OWNER, m1, "close")
    m2 = make_match(name="Two")
    ud = {}
    state = run(registration.joinmatch(group_msg(GROUP_B, 1), mk_ctx(tg, ud, None, [str(m2)])))
    assert state == registration.CONFIRM and ud["reg"]["ign"] == "Alpha"


def test_concurrent_joins_never_exceed_capacity(rs, make_match):
    mid = make_match(capacity=7)
    ok, errs = [], []

    def worker(i):
        try:
            rs.register(mid, 9000 + i, f"u{i}", f"Racer{i:02d}", str(500000000 + i))
            ok.append(i)
        except ServiceError as exc:
            errs.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    v = rs.slots_view(mid)
    assert len(ok) == 7 and v["registered"] == 7 and v["remaining"] == 0 and errs.count("full") == 23


# ---------------- announcements ----------------
def test_new_match_announced_in_every_authorized_group_with_join_button(tg, make_match, db):
    mid = make_match(capacity=48, mode="team", teams=12, size=4, name="Friday")
    bot = mk_bot()
    res = run(announcements.announce_new_match(bot, tg, mid))
    assert res == {GROUP_A: True, GROUP_B: True}
    sent = {c.args[0]: c for c in bot.send_message.call_args_list}
    assert set(sent) == {GROUP_A, GROUP_B}
    text = sent[GROUP_A].args[1]
    for needle in (f"#{mid}", "Mode: Squad", "Registered: 0/48", "Slots remaining: 48", "Registration Open",
                   f"/joinmatch {mid}"):
        assert needle in text
    assert sent[GROUP_A].kwargs["reply_markup"] is not None
    with db.read() as conn:
        assert repo.get_announcement(conn, mid, GROUP_A) and repo.get_announcement(conn, mid, GROUP_B)
        assert repo.get_announcement(conn, mid, -1003) is None


def test_refresh_edits_in_place_and_survives_edit_failures(tg, make_match, rs, db):
    mid = make_match(capacity=5)
    bot = mk_bot()
    run(announcements.announce_new_match(bot, tg, mid))
    sends = bot.send_message.call_count
    register_n(rs, mid, 2)
    run(announcements.refresh_announcements(bot, tg, mid))
    assert bot.send_message.call_count == sends  # edited, not re-posted
    assert bot.edit_message_text.call_count == 2
    assert "Registered: 2/5" in bot.edit_message_text.call_args.args[0]
    # "message is not modified" is harmless
    run(announcements.refresh_announcements(mk_bot(edit_error=BadRequest("Message is not modified")), tg, mid))
    # message deleted -> a new announcement is posted and remembered
    bot2 = mk_bot(edit_error=BadRequest("Message to edit not found"))
    run(announcements.refresh_announcements(bot2, tg, mid))
    assert bot2.send_message.call_count == 2
    # no permission in one group does not stop the other
    bot3 = mk_bot(fail_chats=(GROUP_A,), edit_error=Forbidden("kicked"))
    run(announcements.refresh_announcements(bot3, tg, mid))


def test_failed_group_does_not_break_creation_announcement(tg, make_match):
    mid = make_match()
    bot = mk_bot(fail_chats=(GROUP_A,))
    res = run(announcements.announce_new_match(bot, tg, mid))
    assert res == {GROUP_A: False, GROUP_B: True}


def test_full_match_hides_join_button(tg, make_match, rs):
    mid = make_match(capacity=1)
    bot = mk_bot()
    run(announcements.announce_new_match(bot, tg, mid))
    register_n(rs, mid, 1)
    run(announcements.refresh_announcements(bot, tg, mid))
    kw = bot.edit_message_text.call_args.kwargs
    assert kw["reply_markup"] is None and "Full" in bot.edit_message_text.call_args.args[0]


def test_status_changes_notify_and_refresh(tg, make_match, ms):
    mid = make_match(open_reg=False)
    bot = mk_bot()
    run(announcements.announce_new_match(bot, tg, mid))
    assert "Draft" in bot.send_message.call_args.args[1] and bot.send_message.call_args.kwargs["reply_markup"] is None
    m = ms.transition(OWNER, mid, "open")
    run(announcements.after_status_change(bot, tg, m))
    assert "Registration Open" in bot.edit_message_text.call_args.args[0]
    assert bot.edit_message_text.call_args.kwargs["reply_markup"] is not None
    assert "open" in bot.send_message.call_args.args[1]  # one short notice per group


def test_admin_open_button_announces_and_setslots_refreshes(tg, make_match, ms):
    mid = make_match(open_reg=False)
    bot = mk_bot()
    run(announcements.announce_new_match(bot, tg, mid))
    press(tg, {}, f"adm:ms:{mid}:open", bot)
    assert ms.get(mid)["status"] == "registration_open" and bot.edit_message_text.call_count == 2
    ctx = mk_ctx(tg, {}, bot, [str(mid), "10"])
    u = msg_update("/setslots")
    run(slots.setslots(u, ctx))
    assert "Registered: 0/10" in bot.edit_message_text.call_args.args[0]


# ---------------- migration ----------------
def test_migration_2_preserves_existing_data(tmp_path):
    db = Database(str(tmp_path / "old.db"))
    with db.transaction() as conn:  # a database as created by the previous version (schema v1)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        for stmt in MIGRATIONS[1]:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_version VALUES (1)")
        repo.upsert_group(conn, -5, "Old", True)
        mid = repo.insert_match(conn, group_id=-5, name="Old match", mode="solo", start_utc="2030-01-01T10:00:00Z",
                                timezone="Asia/Kolkata", capacity=4, team_count=0, team_size=0, reg_deadline_utc=None,
                                allow_cancel=1, allow_switch=1, created_by=1)
        repo.insert_registration(conn, mid, 7, "u", "Keeper", "123456789", 1)
    assert run_migrations(db) == 2
    with db.read() as conn:
        assert repo.get_reg_by_user(conn, mid, 7)["ign"] == "Keeper"
        assert conn.execute("SELECT COUNT(*) AS c FROM match_announcements").fetchone()["c"] == 0
    assert run_migrations(db) == 2  # idempotent
