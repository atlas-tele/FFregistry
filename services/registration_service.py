"""Registration, slot allocation and slot administration."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import List, Optional

from database import Database
from database import repositories as repo
from .errors import ServiceError
from .match_service import OPEN, CLOSED, DONE, CANCELLED, utcnow

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def validate_ign(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not 2 <= len(name) <= 20:
        raise ServiceError("In-game name must be 2-20 characters.", "invalid_name")
    if _CTRL.search(name) or name.startswith("/"):
        raise ServiceError("In-game name contains invalid characters.", "invalid_name")
    return name


def validate_uid(raw: str) -> str:
    uid = (raw or "").strip()
    if not uid.isascii() or not uid.isdigit() or not 8 <= len(uid) <= 12:
        raise ServiceError("UID must be 8-12 digits (numbers only).", "invalid_uid")
    if uid.startswith("0"):
        raise ServiceError("UID cannot start with 0.", "invalid_uid")
    return uid


def compress_ranges(numbers: List[int]) -> str:
    """[19,20,21,25] -> 'Slot 19-21, Slot 25'."""
    out, i = [], 0
    nums = sorted(numbers)
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"Slot {nums[i]}" if i == j else f"Slot {nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


class RegistrationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(self, match_id: int, telegram_id: int, username: Optional[str], ign: str, uid: str,
                 now: Optional[datetime] = None):
        ign, uid = validate_ign(ign), validate_uid(uid)
        now = now or utcnow()
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            if m["status"] != OPEN:
                raise ServiceError("Registration is not open for this match.", "closed")
            if m["reg_deadline_utc"]:
                deadline = datetime.strptime(m["reg_deadline_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
                if now > deadline:
                    raise ServiceError("The registration deadline has passed.", "deadline")
            if repo.get_reg_by_user(conn, match_id, telegram_id):
                raise ServiceError("You are already registered for this match. Use /myregistration.", "duplicate")
            if repo.get_reg_by_uid(conn, match_id, uid):
                raise ServiceError("That UID is already registered for this match.", "duplicate_uid")
            taken = {r["slot"] for r in repo.list_registrations(conn, match_id)}
            slot = next((s for s in range(1, m["capacity"] + 1) if s not in taken), None)
            if slot is None:
                raise ServiceError("This match is full. 😔", "full")
            try:
                rid = repo.insert_registration(conn, match_id, telegram_id, username, ign, uid, slot)
            except sqlite3.IntegrityError:  # belt and braces; BEGIN IMMEDIATE already serialises
                raise ServiceError("Could not complete registration. Please try again.", "conflict")
            return repo.get_registration(conn, rid), m

    def get_user_registration(self, match_id: int, telegram_id: int):
        with self.db.read() as conn:
            return repo.get_reg_by_user(conn, match_id, telegram_id)

    def cancel_own(self, match_id: int, telegram_id: int):
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            reg = repo.get_reg_by_user(conn, match_id, telegram_id)
            if not m or not reg:
                raise ServiceError("You are not registered for this match.", "not_registered")
            if not m["allow_cancel"]:
                raise ServiceError("Cancelling registration is disabled for this match. Ask an admin.", "no_cancel")
            if m["status"] not in (OPEN, CLOSED):
                raise ServiceError("Registration can no longer be cancelled.", "locked")
            self._delete(conn, reg)
            repo.add_audit(conn, telegram_id, match_id, "self_cancel", reg["uid"], reg["ign"])
            return reg

    def admin_remove(self, admin_id: int, match_id: int, reg_id: int):
        with self.db.transaction() as conn:
            reg = repo.get_registration(conn, reg_id)
            if not reg or reg["match_id"] != match_id:
                raise ServiceError("Player not found in this match.", "not_found")
            self._delete(conn, reg)
            repo.add_audit(conn, admin_id, match_id, "player_remove", reg["uid"], reg["ign"])
            return reg

    @staticmethod
    def _delete(conn, reg) -> None:
        if conn.execute("SELECT 1 FROM results WHERE registration_id=?", (reg["id"],)).fetchone():
            raise ServiceError("This player has a saved result. Delete the result first.", "has_result")
        repo.delete_registration(conn, reg["id"])  # memberships cascade; slot is released

    def admin_set_slot(self, admin_id: int, match_id: int, reg_id: int, slot: int):
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            reg = repo.get_registration(conn, reg_id)
            if not m or not reg or reg["match_id"] != match_id:
                raise ServiceError("Player not found in this match.", "not_found")
            if not 1 <= slot <= m["capacity"]:
                raise ServiceError(f"Slot must be between 1 and {m['capacity']}.")
            other = repo.get_reg_by_slot(conn, match_id, slot)
            if other and other["id"] != reg_id:
                raise ServiceError(f"Slot {slot} is taken by {other['ign']}. Pick an empty slot.", "slot_taken")
            repo.update_slot(conn, reg_id, slot)
            repo.add_audit(conn, admin_id, match_id, "slot_assign", reg["uid"], f"{reg['slot']}->{slot}")

    def set_capacity(self, admin_id: int, match_id: int, capacity: int):
        from .match_service import MatchService
        m = MatchService(self.db).get(match_id)
        if m["mode"] == "team":
            raise ServiceError("In team mode capacity = teams x team size. Change team count/size instead.")
        return MatchService(self.db).update_settings(admin_id, match_id, capacity=capacity)

    # -- views --------------------------------------------------------
    def slots_view(self, match_id: int) -> dict:
        with self.db.read() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError("Match not found.", "not_found")
            regs = repo.list_registrations(conn, match_id)
        taken = {r["slot"] for r in regs}
        empty = [s for s in range(1, m["capacity"] + 1) if s not in taken]
        return {"match": m, "regs": regs, "total": m["capacity"], "registered": len(regs),
                "occupied": len(regs), "empty_slots": empty, "empty": len(empty),
                "over_capacity": max(0, len(regs) - m["capacity"])}

    def search(self, match_id: int, term: str):
        with self.db.read() as conn:
            return repo.search_registrations(conn, match_id, term.strip())

    def find_by_uid(self, match_id: int, uid: str):
        with self.db.read() as conn:
            return repo.get_reg_by_uid(conn, match_id, uid.strip())

    def export_csv(self, admin_id: int, match_id: int) -> str:
        import csv
        import io
        with self.db.transaction() as conn:
            regs = repo.list_registrations(conn, match_id)
            teams = {t["id"]: t["name"] for t in repo.list_teams(conn, match_id)}
            member = {r["registration_id"]: teams.get(r["team_id"], "") for r in conn.execute(
                "SELECT registration_id, team_id FROM team_memberships WHERE match_id=?", (match_id,))}
            repo.add_audit(conn, admin_id, match_id, "export_csv", "", str(len(regs)))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["slot", "ign", "uid", "telegram_id", "username", "team", "registered_at"])
        for r in regs:
            w.writerow([r["slot"], _csv_safe(r["ign"]), r["uid"], r["telegram_id"],
                        _csv_safe(r["username"] or ""), member.get(r["id"], ""), r["created_at"]])
        return buf.getvalue()


def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
