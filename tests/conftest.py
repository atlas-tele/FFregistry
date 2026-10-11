import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database, run_migrations  # noqa: E402
from database import repositories as repo  # noqa: E402
from services.match_service import MatchService  # noqa: E402
from services.permission_service import PermissionService  # noqa: E402
from services.registration_service import RegistrationService  # noqa: E402
from services.result_service import ResultService  # noqa: E402
from services.team_service import TeamService  # noqa: E402

GROUP_A, GROUP_B = -1001, -1002
OWNER, ADMIN, PLAYER = 111, 222, 999


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    run_migrations(d)
    with d.transaction() as conn:
        repo.upsert_group(conn, GROUP_A, "Group A", True)
        repo.upsert_group(conn, GROUP_B, "Group B", True)
        repo.upsert_group(conn, -1003, "Unauthorized", False)
    return d


@pytest.fixture
def ms(db):
    return MatchService(db)


@pytest.fixture
def rs(db):
    return RegistrationService(db)


@pytest.fixture
def ts(db):
    return TeamService(db)


@pytest.fixture
def res(db):
    return ResultService(db)


@pytest.fixture
def perms(db):
    return PermissionService(db, OWNER, [ADMIN])


@pytest.fixture
def make_match(ms):
    def _make(group=GROUP_A, mode="solo", capacity=4, teams=0, size=0, open_reg=True, name="Friday Night Custom"):
        mid = ms.create_match(OWNER, group, name, mode, "2030-01-01 21:00", "Asia/Kolkata",
                              capacity, teams, size)
        if open_reg:
            ms.transition(OWNER, mid, "open")
        return mid
    return _make


def register_n(rs, mid, n, start=1):
    out = []
    for i in range(start, start + n):
        reg, _ = rs.register(mid, 1000 + i, f"user{i}", f"Player{i}", str(100000000 + i))
        out.append(reg)
    return out


@pytest.fixture
def tg(db):
    """Application container for handler tests (needs python-telegram-bot installed)."""
    from config import Config
    from handlers.common import build_app
    cfg = Config("123:TESTTOKEN", OWNER, frozenset({ADMIN}), db.path, "Asia/Kolkata", frozenset(), "INFO")
    return build_app(cfg, db)
