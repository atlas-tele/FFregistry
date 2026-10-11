"""Match creation, configuration and lifecycle."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import Database
from database import repositories as repo
from .errors import ServiceError
from .scoring_service import DEFAULT_RULES, Rules

MAX_CAPACITY = 100
MODES = ("solo", "team", "custom")
OPEN, CLOSED, PROGRESS, DONE, CANCELLED, DRAFT = (
    "registration_open", "registration_closed", "in_progress", "completed", "cancelled", "draft")
STATUS_LABEL = {DRAFT: "📝 Draft", OPEN: "🟢 Registration open", CLOSED: "🔒 Registration closed",
                PROGRESS: "🎮 In progress", DONE: "🏁 Completed", CANCELLED: "❌ Cancelled"}
TRANSITIONS = {
    "open": ({DRAFT, CLOSED}, OPEN),
    "close": ({OPEN}, CLOSED),
    "start": ({DRAFT, OPEN, CLOSED}, PROGRESS),
    "end": ({PROGRESS, CLOSED}, DONE),
    "cancel": ({DRAFT, OPEN, CLOSED, PROGRESS}, CANCELLED),
}
LIVE_STATUSES = (DRAFT, OPEN, CLOSED, PROGRESS)


def validate_config(mode: str, capacity: Optional[int], team_count: int, team_size: int) -> tuple:
    """Return normalised (capacity, team_count, team_size) or raise ServiceError."""
    if mode not in MODES:
        raise ServiceError("Mode must be solo, team or custom.")
    if team_count < 0 or team_size < 0:
        raise ServiceError("Team count and team size cannot be negative.")
    if mode == "solo":
        team_count = team_size = 0
    elif mode == "team":
        if team_count < 1 or team_size < 1:
            raise ServiceError("Team mode needs at least 1 team and a team size of at least 1.")
        capacity = team_count * team_size
    else:  # custom
        if (team_count == 0) != (team_size == 0):
            raise ServiceError("Custom mode: set both team count and team size, or both to 0 for no teams.")
        if capacity is None:
            capacity = team_count * team_size
    if capacity is None or capacity < 1:
        raise ServiceError("Capacity must be at least 1.")
    if capacity > MAX_CAPACITY:
        raise ServiceError(f"Capacity cannot exceed {MAX_CAPACITY}.")
    if team_count and capacity > team_count * team_size:
        raise ServiceError(f"Capacity {capacity} is larger than teams x size ({team_count}x{team_size}"
                           f"={team_count * team_size}).")
    return capacity, team_count, team_size


def is_team_match(match) -> bool:
    return match["team_count"] > 0


def parse_local_datetime(text: str, tz_name: str) -> str:
    """'YYYY-MM-DD HH:MM' in tz_name -> UTC ISO string."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ServiceError(f"Unknown timezone: {tz_name}")
    try:
        local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except ValueError:
        raise ServiceError("Use the format YYYY-MM-DD HH:MM (24-hour), e.g. 2026-10-16 21:00")
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_time(utc_iso: str, tz_name: str, with_date: bool = True) -> str:
    dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    dt = dt.astimezone(ZoneInfo(tz_name))
    clock = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%d %b %Y')}, {clock}" if with_date else clock


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MatchService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- reading ------------------------------------------------------
    def get(self, match_id: int):
        with self.db.read() as conn:
            m = repo.get_match(conn, match_id)
        if not m:
            raise ServiceError(f"Match {match_id} was not found.", "not_found")
        return m

    def get_for_group(self, match_id: int, group_id: int):
        m = self.get(match_id)
        if m["group_id"] != group_id:
            raise ServiceError(f"Match {match_id} was not found in this group.", "not_found")
        return m

    def list(self, group_id: Optional[int] = None, statuses=None, limit: int = 30):
        with self.db.read() as conn:
            return repo.list_matches(conn, group_id, statuses, limit)

    def active_for_group(self, group_id: int):
        """Open registration wins, then in progress, then closed, then draft-less latest."""
        with self.db.read() as conn:
            for st in (OPEN, PROGRESS, CLOSED):
                rows = repo.list_matches(conn, group_id, [st], 1)
                if rows:
                    return rows[0]
        return None

    def latest_completed(self, group_id: int):
        with self.db.read() as conn:
            rows = repo.list_matches(conn, group_id, [DONE], 1)
        return rows[0] if rows else None

    def rules(self, match_id: int) -> Rules:
        with self.db.read() as conn:
            row = repo.get_rules_row(conn, match_id)
        return Rules.from_json(row["placement_json"], row["kill_points"]) if row else DEFAULT_RULES

    # -- creation -----------------------------------------------------
    def create_match(self, admin_id: int, group_id: int, name: str, mode: str, start_local: str,
                     tz_name: str, capacity: Optional[int], team_count: int = 0, team_size: int = 0,
                     deadline_local: Optional[str] = None) -> int:
        name = (name or "").strip()
        if not 2 <= len(name) <= 60:
            raise ServiceError("Match name must be 2-60 characters.")
        capacity, team_count, team_size = validate_config(mode, capacity, team_count, team_size)
        start_utc = parse_local_datetime(start_local, tz_name)
        deadline_utc = parse_local_datetime(deadline_local, tz_name) if deadline_local else None
        if deadline_utc and deadline_utc > start_utc:
            raise ServiceError("The registration deadline must be before the match start.")
        with self.db.transaction() as conn:
            g = repo.get_group(conn, group_id)
            if not g or not g["authorized"]:
                raise ServiceError("That group is not authorized.", "group")
            mid = repo.insert_match(
                conn, group_id=group_id, name=name, mode=mode, start_utc=start_utc, timezone=tz_name,
                capacity=capacity, team_count=team_count, team_size=team_size,
                reg_deadline_utc=deadline_utc, allow_cancel=1, allow_switch=1, created_by=admin_id)
            repo.set_rules(conn, mid, DEFAULT_RULES.to_json(), DEFAULT_RULES.kill_points)
            for n in range(1, team_count + 1):
                repo.insert_team(conn, mid, n, f"Team {n}")
            repo.add_audit(conn, admin_id, mid, "match_create", name, json.dumps(
                {"mode": mode, "capacity": capacity, "teams": team_count, "size": team_size}))
        return mid

    # -- lifecycle ----------------------------------------------------
    def transition(self, admin_id: int, match_id: int, action: str):
        if action not in TRANSITIONS:
            raise ServiceError("Unknown action.")
        allowed, target = TRANSITIONS[action]
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError(f"Match {match_id} was not found.", "not_found")
            if m["status"] not in allowed:
                raise ServiceError(f"Cannot {action} a match that is: {STATUS_LABEL[m['status']]}.")
            if action == "open":
                clash = repo.list_matches(conn, m["group_id"], [OPEN], 5)
                if any(c["id"] != match_id for c in clash):
                    raise ServiceError("Another match in this group already has registration open. Close it first.")
            repo.update_match(conn, match_id, status=target)
            repo.add_audit(conn, admin_id, match_id, f"match_{action}", m["status"], target)
            return repo.get_match(conn, match_id)

    def update_settings(self, admin_id: int, match_id: int, **changes):
        """Update name/mode/capacity/team_count/team_size/time/timezone/deadline/toggles atomically."""
        with self.db.transaction() as conn:
            m = repo.get_match(conn, match_id)
            if not m:
                raise ServiceError(f"Match {match_id} was not found.", "not_found")
            if m["status"] in (DONE, CANCELLED):
                raise ServiceError("Finished or cancelled matches cannot be changed.")
            new = dict(m)
            new.update({k: v for k, v in changes.items() if v is not None or k == "reg_deadline_utc"})
            capacity, tc, ts = validate_config(new["mode"], new["capacity"], new["team_count"], new["team_size"])
            if "mode" in changes or "team_count" in changes or "team_size" in changes:
                self._check_structure_change(conn, m, tc, ts)
            if (changes.get("mode") and changes["mode"] != m["mode"]) and repo.count_results(conn, match_id):
                raise ServiceError("Delete existing results before changing the mode.")
            new.update(capacity=capacity, team_count=tc, team_size=ts)
            if new["reg_deadline_utc"] and new["reg_deadline_utc"] > new["start_utc"]:
                raise ServiceError("The registration deadline must be before the match start.")
            fields = {k: new[k] for k in ("name", "mode", "start_utc", "timezone", "capacity",
                                          "team_count", "team_size", "reg_deadline_utc",
                                          "allow_cancel", "allow_switch")}
            repo.update_match(conn, match_id, **fields)
            for n in range(1, tc + 1):
                repo.insert_team(conn, match_id, n, f"Team {n}")
            repo.delete_teams_above(conn, match_id, tc)
            repo.add_audit(conn, admin_id, match_id, "match_settings", "", json.dumps(
                {k: v for k, v in changes.items() if v is not None}))
            return repo.get_match(conn, match_id)

    @staticmethod
    def _check_structure_change(conn, m, new_tc: int, new_ts: int) -> None:
        if (new_tc, new_ts) == (m["team_count"], m["team_size"]):
            return
        if new_tc == 0 and repo.count_memberships(conn, m["id"]):
            raise ServiceError("Players are on teams. Remove them from teams before disabling teams.")
        if new_tc and repo.max_team_fill(conn, m["id"]) > new_ts:
            raise ServiceError("A team currently has more players than the new team size.")
        for t in repo.list_teams(conn, m["id"]):
            if t["number"] > new_tc and t["members"]:
                raise ServiceError(f"Team {t['number']} still has players. Move them first.")
        if repo.count_results(conn, m["id"]) and (new_tc > 0) != (m["team_count"] > 0):
            raise ServiceError("Delete existing results before switching between team and individual play.")

    def set_rules(self, admin_id: int, match_id: int, rules: Rules) -> None:
        from .result_service import recalc_scores  # local import avoids a cycle
        with self.db.transaction() as conn:
            if not repo.get_match(conn, match_id):
                raise ServiceError(f"Match {match_id} was not found.", "not_found")
            repo.set_rules(conn, match_id, rules.to_json(), rules.kill_points)
            recalc_scores(conn, match_id, rules)
            repo.add_audit(conn, admin_id, match_id, "rules_update", "", rules.to_json())
