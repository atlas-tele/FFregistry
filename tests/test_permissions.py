import pytest

from config import ConfigError, load_config
from services.errors import PermissionDenied, ServiceError
from tests.conftest import ADMIN, GROUP_A, GROUP_B, OWNER, PLAYER


def test_roles(perms):
    assert perms.is_owner(OWNER) and perms.is_admin(OWNER)
    assert perms.is_admin(ADMIN) and not perms.is_owner(ADMIN)
    assert not perms.is_admin(PLAYER) and not perms.is_admin(None)


def test_admin_context_requires_private_chat(perms):
    perms.require_admin_context("private", OWNER)
    for chat in ("group", "supergroup", "channel", None):
        with pytest.raises(PermissionDenied) as ei:
            perms.require_admin_context(chat, OWNER)
        assert ei.value.code == "not_private"


def test_unauthorized_user_rejected_in_private(perms):
    with pytest.raises(PermissionDenied) as ei:
        perms.require_admin_context("private", PLAYER)
    assert ei.value.code == "not_admin"


def test_only_owner_manages_admins(perms):
    with pytest.raises(PermissionDenied):
        perms.add_admin(ADMIN, 555)
    perms.add_admin(OWNER, 555)
    assert perms.is_admin(555)
    with pytest.raises(PermissionDenied):
        perms.remove_admin(ADMIN, 555)
    with pytest.raises(PermissionDenied):
        perms.remove_admin(OWNER, OWNER)
    with pytest.raises(PermissionDenied):
        perms.remove_admin(OWNER, ADMIN)  # env admin: managed via env
    perms.remove_admin(OWNER, 555)
    assert not perms.is_admin(555)
    with pytest.raises(PermissionDenied):
        perms.require_owner_context("private", ADMIN)


def test_admin_by_username_is_impossible(perms):
    assert not perms.is_admin("owner_username")  # type: ignore[arg-type]


def test_group_authorization(perms, ms):
    assert perms.is_group_authorized(GROUP_A)
    assert not perms.is_group_authorized(-1003)
    assert not perms.is_group_authorized(-424242)
    with pytest.raises(ServiceError):
        ms.create_match(OWNER, -1003, "Nope", "solo", "2030-01-01 21:00", "Asia/Kolkata", 4)


def test_match_lookup_is_group_scoped(ms, make_match):
    a = make_match(group=GROUP_A)
    assert ms.get_for_group(a, GROUP_A)["id"] == a
    with pytest.raises(ServiceError):
        ms.get_for_group(a, GROUP_B)


def test_only_one_open_registration_per_group(ms, make_match):
    make_match()
    second = make_match(open_reg=False, name="Second")
    with pytest.raises(ServiceError):
        ms.transition(OWNER, second, "open")
    other_group = make_match(group=GROUP_B)
    assert other_group


def test_lifecycle_rules(ms, make_match):
    mid = make_match(open_reg=False)
    with pytest.raises(ServiceError):
        ms.transition(OWNER, mid, "end")
    ms.transition(OWNER, mid, "open")
    ms.transition(OWNER, mid, "close")
    ms.transition(OWNER, mid, "open")
    ms.transition(OWNER, mid, "start")
    ms.transition(OWNER, mid, "end")
    with pytest.raises(ServiceError):
        ms.transition(OWNER, mid, "cancel")


def test_config_loading():
    env = {"BOT_TOKEN": "x:y", "OWNER_ID": "5", "ADMIN_IDS": "1, 2;3", "TIMEZONE": "Asia/Kolkata"}
    cfg = load_config(env)
    assert cfg.owner_id == 5 and cfg.admin_ids == frozenset({1, 2, 3})
    for bad in ({}, {"BOT_TOKEN": "t", "OWNER_ID": "abc"},
                {"BOT_TOKEN": "t", "OWNER_ID": "1", "ADMIN_IDS": "@name"},
                {"BOT_TOKEN": "t", "OWNER_ID": "1", "TIMEZONE": "Mars/Base"}):
        with pytest.raises(ConfigError):
            load_config(bad)


def test_audit_log_records_admin_actions(db, ms, make_match):
    from database import repositories as repo
    mid = make_match()
    with db.read() as conn:
        actions = [a["action"] for a in repo.list_audit(conn, mid)]
    assert "match_create" in actions and "match_open" in actions
