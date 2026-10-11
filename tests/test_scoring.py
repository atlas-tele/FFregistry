import pytest

from services.errors import ServiceError
from services.scoring_service import (DEFAULT_RULES, Rules, aggregate, calculate,
                                      parse_placement_list, rank_rows)
from tests.conftest import register_n


def test_default_points_table():
    expected = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 48: 0}
    for place, pts in expected.items():
        assert calculate(DEFAULT_RULES, place, 0).placement_points == pts


def test_formula():
    s = calculate(DEFAULT_RULES, 1, 8)
    assert (s.placement_points, s.kill_points, s.total) == (12, 8, 20)
    assert calculate(Rules({1: 10}, 2), 1, 5).total == 20
    assert calculate(DEFAULT_RULES, 30, 3).total == 3


def test_invalid_inputs():
    with pytest.raises(ValueError):
        calculate(DEFAULT_RULES, 0, 1)
    with pytest.raises(ValueError):
        calculate(DEFAULT_RULES, 1, -1)
    with pytest.raises(ValueError):
        parse_placement_list("12,x")
    assert parse_placement_list("5,3,1") == {1: 5, 2: 3, 3: 1}


def _row(key, total, kills, placement, name=None):
    return {"key": key, "name": name or key, "total": total, "kills": kills, "placement": placement}


def test_tiebreakers_and_shared_rank():
    rows = rank_rows([_row("a", 10, 2, 3), _row("b", 10, 5, 4), _row("c", 12, 0, 9),
                      _row("d", 10, 5, 2), _row("e", 10, 5, 2, "E2")])
    order = [(r["key"], r["rank"]) for r in rows]
    assert order[0] == ("c", 1)
    assert order[1][1] == 2 and order[2][1] == 2  # d,e fully tied -> shared rank 2
    assert order[3] == ("b", 4) and order[4] == ("a", 5)


def test_aggregate_by_uid_no_double_count():
    def snap(rows):
        return {"rows": rows}
    p1 = {"uid": "111111111", "name": "Old"}
    p2 = {"uid": "222222222", "name": "Two"}
    s1 = snap([{"key": "111111111", "name": "Old", "total": 10, "kills": 3, "placement": 2, "members": [p1]}])
    s2 = snap([{"key": "T1", "name": "Team 1", "total": 20, "kills": 8, "placement": 1,
                "members": [{"uid": "111111111", "name": "New"}, p2]}])
    out = {r["key"]: r for r in aggregate([s1, s2])}
    assert out["111111111"]["total"] == 30 and out["111111111"]["matches"] == 2
    assert out["111111111"]["name"] == "New" and out["111111111"]["placement"] == 1
    assert out["222222222"]["total"] == 20


# ---------------- results service ----------------
def started(ms, mid):
    ms.transition(111, mid, "start")


def test_submit_edit_delete_and_recalc(ms, rs, res, make_match):
    mid = make_match(capacity=5)
    a, b = register_n(rs, mid, 2)
    with pytest.raises(ServiceError):  # match not started
        res.save(111, mid, a["uid"], 1, 5)
    started(ms, mid)
    label, score = res.save(111, mid, a["uid"], 1, 8)
    assert label == "Player1" and score.total == 20
    with pytest.raises(ServiceError) as ei:
        res.save(111, mid, a["uid"], 2, 1)
    assert ei.value.code == "duplicate"
    with pytest.raises(ServiceError) as ei:
        res.save(111, mid, b["uid"], 1, 1)
    assert ei.value.code == "dup_placement"
    for bad in [(0, 1), (6, 1), (2, -1), (2, 100)]:
        with pytest.raises(ServiceError):
            res.save(111, mid, b["uid"], *bad)
    res.save(111, mid, b["uid"], 2, 3)
    res.save(111, mid, a["uid"], 3, 8, overwrite=True)  # edit placement 1 -> 3
    rows = res.live_snapshot(mid)["rows"]
    assert [(r["key"], r["total"]) for r in rows] == [(a["uid"], 16), (b["uid"], 12)] or \
           [(r["key"], r["total"]) for r in rows] == [(b["uid"], 12), (a["uid"], 16)]
    ms.set_rules(111, mid, Rules({1: 100, 2: 50, 3: 25}, 2))
    totals = {r["key"]: r["total"] for r in res.live_snapshot(mid)["rows"]}
    assert totals == {a["uid"]: 25 + 16, b["uid"]: 50 + 6}
    res.delete(111, mid, b["uid"])
    assert len(res.live_snapshot(mid)["rows"]) == 1
    with pytest.raises(ServiceError):
        res.delete(111, mid, b["uid"])


def test_team_results_and_player_kills(ms, rs, ts, res, make_match):
    mid = make_match(mode="team", teams=2, size=2)
    regs = register_n(rs, mid, 4)
    for i, r in enumerate(regs):
        ts.join(mid, r["telegram_id"], 1 if i < 2 else 2)
    started(ms, mid)
    with pytest.raises(ServiceError):
        res.save(111, mid, regs[0]["uid"], 1, 1)  # must use team token
    res.save(111, mid, "T2", 1, 7)
    res.save(111, mid, "T1", 2, 4)
    res.save_player_kills(111, mid, regs[2]["uid"], 5)
    snap = res.live_snapshot(mid)
    assert [r["name"] for r in snap["rows"]] == ["Team 2", "Team 1"]
    assert snap["rows"][0]["total"] == 19 and snap["player_kills"][0]["kills"] == 5
    assert len(snap["rows"][0]["members"]) == 2


def test_publish_flow_marks_unpublished_changes(ms, rs, res, make_match):
    mid = make_match()
    a, = register_n(rs, mid, 1)
    started(ms, mid)
    res.save(111, mid, a["uid"], 1, 2)
    assert res.published_snapshot(mid) is None and res.has_unpublished_changes(mid)
    res.record_publish(111, mid, -1001, 55, res.live_snapshot(mid))
    assert not res.has_unpublished_changes(mid)
    res.save(111, mid, a["uid"], 2, 2, overwrite=True)
    assert res.has_unpublished_changes(mid)
    assert res.published_snapshot(mid)["rows"][0]["placement"] == 1  # players still see old snapshot


def test_cumulative_counts_completed_published_matches_once_each(ms, rs, res, make_match):
    from tests.conftest import GROUP_A, GROUP_B
    m1 = make_match(group=GROUP_A, name="M1")
    m2 = make_match(group=GROUP_B, name="M2")
    a, = register_n(rs, m1, 1)
    rs.register(m2, 1001, "x", "Player1", a["uid"])
    for mid in (m1, m2):
        started(ms, mid)
        reg = rs.get_user_registration(mid, 1001)
        res.save(111, mid, reg["uid"], 1, 3)
        res.record_publish(111, mid, -1, 1, res.live_snapshot(mid))
    assert res.cumulative(GROUP_A) == []  # not completed yet
    ms.transition(111, m1, "end")
    ms.transition(111, m2, "end")
    out = res.cumulative(GROUP_A)
    assert out == res.cumulative(GROUP_B)  # same shared leaderboard from every group
    assert len(out) == 1 and out[0]["total"] == 30 and out[0]["matches"] == 2


def test_results_block_player_removal(ms, rs, res, make_match):
    mid = make_match()
    a, = register_n(rs, mid, 1)
    started(ms, mid)
    res.save(111, mid, a["uid"], 1, 0)
    with pytest.raises(ServiceError) as ei:
        rs.admin_remove(111, mid, a["id"])
    assert ei.value.code == "has_result"
