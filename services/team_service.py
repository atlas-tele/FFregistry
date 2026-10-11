"""Team membership with capacity enforcement."""
from __future__ import annotations

from database import Database
from database import repositories as repo
from .errors import ServiceError
from .match_service import OPEN, CLOSED

JOINABLE = (OPEN, CLOSED)


class TeamService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _ctx(self, conn, match_id: int, reg_id: int, team_number: int):
        m = repo.get_match(conn, match_id)
        if not m:
            raise ServiceError("Match not found.", "not_found")
        if m["team_count"] == 0:
            raise ServiceError("This match has no teams.", "no_teams")
        reg = repo.get_registration(conn, reg_id)
        if not reg or reg["match_id"] != match_id:
            raise ServiceError("Player not found in this match.", "not_found")
        team = repo.get_team(conn, match_id, team_number)
        if not team:
            raise ServiceError(f"Team {team_number} does not exist (1-{m['team_count']}).", "no_team")
        return m, reg, team

    def _place(self, conn, m, reg, team, actor_checks: bool, admin_id=None):
        current = repo.get_membership(conn, m["id"], reg["id"])
        if current and current["team_id"] == team["id"]:
            raise ServiceError(f"You are already in {team['name']}.", "same_team")
        if current and actor_checks and not m["allow_switch"]:
            raise ServiceError("Switching teams is disabled for this match.", "no_switch")
        if repo.count_team_members(conn, team["id"]) >= m["team_size"]:
            raise ServiceError(f"{team['name']} is full ({m['team_size']}/{m['team_size']}).", "team_full")
        if current:
            repo.move_membership(conn, m["id"], reg["id"], team["id"])
        else:
            repo.add_membership(conn, m["id"], team["id"], reg["id"])
        return current

    def join(self, match_id: int, telegram_id: int, team_number: int):
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            reg = repo.get_reg_by_user(conn, match_id, telegram_id)
            if not reg:
                raise ServiceError("Register first with /register.", "not_registered")
            if m["status"] not in JOINABLE:
                raise ServiceError("Teams are locked for this match.", "locked")
            m, reg, team = self._ctx(conn, match_id, reg["id"], team_number)
            self._place(conn, m, reg, team, True)
            return team, reg

    def leave(self, match_id: int, telegram_id: int):
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            reg = repo.get_reg_by_user(conn, match_id, telegram_id)
            if not m or not reg:
                raise ServiceError("You are not registered for this match.", "not_registered")
            if m["status"] not in JOINABLE:
                raise ServiceError("Teams are locked for this match.", "locked")
            cur = repo.get_membership(conn, match_id, reg["id"])
            if not cur:
                raise ServiceError("You are not in a team.", "no_team")
            if not m["allow_switch"]:
                raise ServiceError("Leaving teams is disabled for this match. Ask an admin.", "no_switch")
            team = repo.get_team_by_id(conn, cur["team_id"])
            repo.delete_membership(conn, match_id, reg["id"])
            return team

    def admin_move(self, admin_id: int, match_id: int, reg_id: int, team_number: int):
        with self.db.transaction() as conn:
            m, reg, team = self._ctx(conn, match_id, reg_id, team_number)
            self._place(conn, m, reg, team, False)
            repo.add_audit(conn, admin_id, match_id, "team_move", reg["uid"], f"->{team['name']}")
            return team, reg

    def admin_remove(self, admin_id: int, match_id: int, reg_id: int):
        with self.db.transaction() as conn:
            reg = repo.get_registration(conn, reg_id)
            if not reg or reg["match_id"] != match_id:
                raise ServiceError("Player not found in this match.", "not_found")
            if not repo.delete_membership(conn, match_id, reg_id):
                raise ServiceError("That player is not in a team.", "no_team")
            repo.add_audit(conn, admin_id, match_id, "team_remove", reg["uid"], reg["ign"])
            return reg

    def view(self, match_id: int) -> dict:
        with self.db.read() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            teams = []
            for t in repo.list_teams(conn, match_id):
                teams.append({"team": t, "members": repo.team_members(conn, t["id"])})
            in_team = {r["registration_id"] for r in conn.execute(
                "SELECT registration_id FROM team_memberships WHERE match_id=?", (match_id,))}
            unassigned = [r for r in repo.list_registrations(conn, match_id) if r["id"] not in in_team]
        return {"match": m, "teams": teams, "unassigned": unassigned}

    def team_of(self, match_id: int, telegram_id: int):
        with self.db.read() as conn:
            reg = repo.get_reg_by_user(conn, match_id, telegram_id)
            if not reg:
                return None, None
            cur = repo.get_membership(conn, match_id, reg["id"])
            if not cur:
                return reg, None
            t = repo.get_team_by_id(conn, cur["team_id"])
            return reg, {"team": t, "members": repo.team_members(conn, t["id"])}
