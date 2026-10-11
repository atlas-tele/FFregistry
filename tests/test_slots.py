import pytest

from services.errors import ServiceError
from services.formatting import slots_page, split_message
from services.registration_service import compress_ranges
from tests.conftest import register_n


def test_counts_come_from_database(rs, make_match):
    mid = make_match(capacity=48)
    register_n(rs, mid, 18)
    v = rs.slots_view(mid)
    assert (v["total"], v["registered"], v["occupied"], v["empty"]) == (48, 18, 18, 30)
    assert v["empty_slots"][0] == 19 and v["empty_slots"][-1] == 48


def test_compress_ranges():
    assert compress_ranges([19, 20, 21, 25]) == "Slot 19-21, Slot 25"
    assert compress_ranges([]) == ""


def test_pagination_and_message_length(rs, make_match):
    mid = make_match(capacity=100)
    register_n(rs, mid, 100)
    v = rs.slots_view(mid)
    text, page, pages = slots_page(v, 1, 20)
    assert pages == 5 and "Slot 20 - Player20" in text and "Slot 21 -" not in text
    text5, _, _ = slots_page(v, 99, 20)
    assert "Slot 100 - Player100" in text5
    assert all(len(c) <= 4000 for c in split_message("x\n" * 5000))


def test_admin_set_slot(rs, make_match):
    mid = make_match(capacity=5)
    a, b = register_n(rs, mid, 2)
    rs.admin_set_slot(111, mid, a["id"], 5)
    assert rs.get_user_registration(mid, a["telegram_id"])["slot"] == 5
    with pytest.raises(ServiceError) as ei:
        rs.admin_set_slot(111, mid, b["id"], 5)
    assert ei.value.code == "slot_taken"
    with pytest.raises(ServiceError):
        rs.admin_set_slot(111, mid, b["id"], 6)


def test_capacity_reduction_keeps_registrations(rs, ms, make_match):
    mid = make_match(capacity=10)
    register_n(rs, mid, 6)
    ms.update_settings(111, mid, capacity=4)
    v = rs.slots_view(mid)
    assert v["registered"] == 6 and v["over_capacity"] == 2 and v["empty"] == 0
    with pytest.raises(ServiceError) as ei:
        rs.register(mid, 9, "z", "Late", "999999999")
    assert ei.value.code == "full"


def test_removal_releases_slot_and_audit(db, rs, make_match):
    from database import repositories as repo
    mid = make_match()
    a, = register_n(rs, mid, 1)
    rs.admin_remove(111, mid, a["id"])
    assert rs.slots_view(mid)["registered"] == 0
    with db.read() as conn:
        assert repo.list_audit(conn, mid)[0]["action"] == "player_remove"


def test_csv_export_neutralises_formulas(rs, make_match):
    mid = make_match()
    rs.register(mid, 1, "a", "=HYPERLINK(1)", "111111111")
    csv_text = rs.export_csv(111, mid)
    assert "'=HYPERLINK(1)" in csv_text and csv_text.startswith("slot,ign,uid")
