import threading

import pytest

from database import Database
from services.errors import ServiceError
from services.registration_service import validate_ign, validate_uid
from tests.conftest import GROUP_A, GROUP_B, register_n


def test_successful_registration(rs, make_match):
    mid = make_match()
    reg, m = rs.register(mid, 1, "alpha", "PlayerAlpha", "123456789")
    assert reg["slot"] == 1 and reg["ign"] == "PlayerAlpha" and reg["uid"] == "123456789"
    assert m["name"] == "Friday Night Custom"


def test_invalid_names():
    for bad in ["", " ", "a", "x" * 21, "/cmd", "tab\x07bell"]:
        with pytest.raises(ServiceError):
            validate_ign(bad)
    assert validate_ign("  Good   Name ") == "Good Name"


def test_invalid_uids():
    for bad in ["", "abc", "1234567", "1" * 13, "0123456789", "12 345 678", "١٢٣٤٥٦٧٨٩"]:
        with pytest.raises(ServiceError):
            validate_uid(bad)
    assert validate_uid(" 123456789 ") == "123456789"


def test_duplicate_telegram_account(rs, make_match):
    mid = make_match()
    rs.register(mid, 1, "a", "Alpha", "111111111")
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 1, "a", "Alpha2", "222222222")
    assert ei.value.code == "duplicate"


def test_duplicate_uid(rs, make_match):
    mid = make_match()
    rs.register(mid, 1, "a", "Alpha", "111111111")
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 2, "b", "Beta", "111111111")
    assert ei.value.code == "duplicate_uid"


def test_full_room(rs, make_match):
    mid = make_match(capacity=2)
    register_n(rs, mid, 2)
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 5, "x", "Extra", "555555555")
    assert ei.value.code == "full"


def test_registration_requires_open(rs, make_match, ms):
    mid = make_match(open_reg=False)
    with pytest.raises(ServiceError):
        rs.register(mid, 1, "a", "Alpha", "111111111")
    ms.transition(111, mid, "open")
    rs.register(mid, 1, "a", "Alpha", "111111111")
    ms.transition(111, mid, "close")
    with pytest.raises(ServiceError):
        rs.register(mid, 2, "b", "Beta", "222222222")


def test_unique_slots_and_reuse_after_removal(rs, make_match):
    mid = make_match(capacity=5)
    regs = register_n(rs, mid, 3)
    assert [r["slot"] for r in regs] == [1, 2, 3]
    rs.admin_remove(111, mid, regs[1]["id"])
    reg, _ = rs.register(mid, 77, "n", "NewGuy", "777777777")
    assert reg["slot"] == 2
    slots = [r["slot"] for r in rs.slots_view(mid)["regs"]]
    assert len(slots) == len(set(slots))


def test_concurrent_registration_is_safe(rs, make_match):
    mid = make_match(capacity=10)
    errors, ok = [], []

    def worker(i):
        try:
            reg, _ = rs.register(mid, 5000 + i, f"u{i}", f"Racer{i:02d}", str(300000000 + i))
            ok.append(reg["slot"])
        except ServiceError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 10 and len(set(ok)) == 10
    assert sorted(ok) == list(range(1, 11))
    assert errors.count("full") == 15


def test_concurrent_same_user_only_once(rs, make_match):
    mid = make_match(capacity=10)
    ok = []

    def worker(i):
        try:
            rs.register(mid, 42, "same", "SameUser", str(400000000 + i))
            ok.append(i)
        except ServiceError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1


def test_self_cancel_respects_settings(rs, make_match, ms):
    mid = make_match()
    rs.register(mid, 1, "a", "Alpha", "111111111")
    ms.update_settings(111, mid, allow_cancel=0)
    with pytest.raises(ServiceError) as ei:
        rs.cancel_own(mid, 1)
    assert ei.value.code == "no_cancel"
    ms.update_settings(111, mid, allow_cancel=1)
    rs.cancel_own(mid, 1)
    assert rs.get_user_registration(mid, 1) is None


def test_match_is_shared_by_all_groups(rs, ms, make_match):
    """One match record: a player registered from group A cannot register again from group B."""
    mid = make_match(group=GROUP_A)
    assert ms.get_for_group(mid, GROUP_B)["id"] == mid  # visible from the other authorized group
    rs.register(mid, 1, "a", "Alpha", "111111111")
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 1, "a", "Alpha", "111111111")
    assert ei.value.code == "duplicate"
    assert rs.slots_view(mid)["registered"] == 1


def test_persistence_after_restart(tmp_path, ms, rs, make_match):
    mid = make_match()
    rs.register(mid, 1, "a", "Alpha", "111111111")
    reopened = Database(rs.db.path)
    from database import run_migrations
    run_migrations(reopened)
    from services.registration_service import RegistrationService
    assert RegistrationService(reopened).get_user_registration(mid, 1)["ign"] == "Alpha"


def test_deadline(rs, ms):
    mid = ms.create_match(111, GROUP_A, "DL", "solo", "2030-01-01 21:00", "Asia/Kolkata", 4,
                          deadline_local="2030-01-01 20:00")
    ms.transition(111, mid, "open")
    from datetime import datetime, timezone
    late = datetime(2030, 1, 1, 16, 0, tzinfo=timezone.utc)  # 21:30 IST
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 1, "a", "Alpha", "111111111", now=late)
    assert ei.value.code == "deadline"
