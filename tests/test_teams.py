import pytest

from services.errors import ServiceError
from tests.conftest import register_n


def test_team_capacity_not_hardcoded(ts, rs, make_match):
    mid = make_match(mode="team", teams=7, size=3)
    assert len(ts.view(mid)["teams"]) == 7
    regs = register_n(rs, mid, 4)
    for r in regs[:3]:
        ts.join(mid, r["telegram_id"], 7)
    with pytest.raises(ServiceError) as ei:
        ts.join(mid, regs[3]["telegram_id"], 7)
    assert ei.value.code == "team_full"
    with pytest.raises(ServiceError):
        ts.join(mid, regs[3]["telegram_id"], 8)


def test_join_requires_registration_and_single_team(ts, rs, make_match):
    mid = make_match(mode="team", teams=2, size=2)
    with pytest.raises(ServiceError) as ei:
        ts.join(mid, 5, 1)
    assert ei.value.code == "not_registered"
    r, = register_n(rs, mid, 1)
    ts.join(mid, r["telegram_id"], 1)
    with pytest.raises(ServiceError) as ei:
        ts.join(mid, r["telegram_id"], 1)
    assert ei.value.code == "same_team"
    ts.join(mid, r["telegram_id"], 2)  # switch
    v = ts.view(mid)
    assert [len(t["members"]) for t in v["teams"]] == [0, 1]


def test_leave_and_switch_rules(ts, rs, ms, make_match):
    mid = make_match(mode="team", teams=2, size=2)
    r, = register_n(rs, mid, 1)
    ts.join(mid, r["telegram_id"], 1)
    ms.update_settings(111, mid, allow_switch=0)
    with pytest.raises(ServiceError) as ei:
        ts.join(mid, r["telegram_id"], 2)
    assert ei.value.code == "no_switch"
    with pytest.raises(ServiceError):
        ts.leave(mid, r["telegram_id"])
    ms.update_settings(111, mid, allow_switch=1)
    ts.leave(mid, r["telegram_id"])
    with pytest.raises(ServiceError):
        ts.leave(mid, r["telegram_id"])


def test_solo_match_has_no_teams(ts, rs, make_match):
    mid = make_match()
    r, = register_n(rs, mid, 1)
    with pytest.raises(ServiceError) as ei:
        ts.join(mid, r["telegram_id"], 1)
    assert ei.value.code == "no_teams"


def test_admin_move_and_remove(ts, rs, make_match):
    mid = make_match(mode="team", teams=2, size=1)
    a, b = register_n(rs, mid, 2)
    ts.join(mid, a["telegram_id"], 1)
    with pytest.raises(ServiceError):
        ts.admin_move(111, mid, b["id"], 1)  # full
    ts.admin_move(111, mid, b["id"], 2)
    ts.admin_remove(111, mid, b["id"])
    with pytest.raises(ServiceError):
        ts.admin_remove(111, mid, b["id"])


def test_custom_mode_validation(ms):
    from services.match_service import validate_config
    assert validate_config("custom", 10, 0, 0) == (10, 0, 0)
    assert validate_config("custom", 6, 3, 2) == (6, 3, 2)
    for bad in [("custom", 10, 3, 2), ("custom", 6, 3, 0), ("solo", 0, 0, 0), ("team", None, 0, 4),
                ("solo", 101, 0, 0), ("weird", 5, 0, 0)]:
        with pytest.raises(Exception):
            validate_config(*bad)
    assert validate_config("team", None, 12, 4) == (48, 12, 4)


def test_team_removal_cascades_with_player(ts, rs, make_match):
    mid = make_match(mode="team", teams=2, size=2)
    a, = register_n(rs, mid, 1)
    ts.join(mid, a["telegram_id"], 1)
    rs.admin_remove(111, mid, a["id"])
    assert ts.view(mid)["teams"][0]["members"] == []


def test_shrinking_team_size_below_fill_is_refused(ms, ts, rs, make_match):
    mid = make_match(mode="team", teams=2, size=3)
    for r in register_n(rs, mid, 3):
        ts.join(mid, r["telegram_id"], 1)
    with pytest.raises(ServiceError):
        ms.update_settings(111, mid, team_size=2)
