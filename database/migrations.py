"""Versioned schema migrations, applied automatically at startup."""
from __future__ import annotations

import logging

from .connection import Database

log = logging.getLogger(__name__)

STATUSES = "'draft','registration_open','registration_closed','in_progress','completed','cancelled'"

MIGRATIONS: dict = {
    1: [
        """CREATE TABLE groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            authorized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL)""",
        f"""CREATE TABLE matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(chat_id),
            name TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('solo','team','custom')),
            start_utc TEXT NOT NULL,
            timezone TEXT NOT NULL,
            capacity INTEGER NOT NULL CHECK (capacity > 0),
            team_count INTEGER NOT NULL DEFAULT 0 CHECK (team_count >= 0),
            team_size INTEGER NOT NULL DEFAULT 0 CHECK (team_size >= 0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ({STATUSES})),
            reg_deadline_utc TEXT,
            allow_cancel INTEGER NOT NULL DEFAULT 1,
            allow_switch INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX idx_matches_group ON matches(group_id, status)",
        """CREATE TABLE registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id),
            telegram_id INTEGER NOT NULL,
            username TEXT,
            ign TEXT NOT NULL,
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL CHECK (slot > 0),
            created_at TEXT NOT NULL,
            UNIQUE (match_id, telegram_id),
            UNIQUE (match_id, slot),
            UNIQUE (match_id, uid))""",
        "CREATE INDEX idx_reg_match ON registrations(match_id)",
        """CREATE TABLE teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id),
            number INTEGER NOT NULL CHECK (number > 0),
            name TEXT NOT NULL,
            UNIQUE (match_id, number))""",
        """CREATE TABLE team_memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id),
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            UNIQUE (match_id, registration_id))""",
        "CREATE INDEX idx_tm_team ON team_memberships(team_id)",
        """CREATE TABLE scoring_rules (
            match_id INTEGER PRIMARY KEY REFERENCES matches(id),
            placement_json TEXT NOT NULL,
            kill_points INTEGER NOT NULL)""",
        """CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id),
            registration_id INTEGER REFERENCES registrations(id),
            team_id INTEGER REFERENCES teams(id),
            placement INTEGER NOT NULL CHECK (placement >= 1),
            kills INTEGER NOT NULL CHECK (kills >= 0),
            placement_points INTEGER NOT NULL,
            kill_points INTEGER NOT NULL,
            total INTEGER NOT NULL,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK ((registration_id IS NULL) != (team_id IS NULL)),
            UNIQUE (match_id, registration_id),
            UNIQUE (match_id, team_id),
            UNIQUE (match_id, placement))""",
        """CREATE TABLE player_kills (
            match_id INTEGER NOT NULL REFERENCES matches(id),
            registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
            kills INTEGER NOT NULL CHECK (kills >= 0),
            PRIMARY KEY (match_id, registration_id))""",
        """CREATE TABLE published_results (
            match_id INTEGER PRIMARY KEY REFERENCES matches(id),
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            published_at TEXT NOT NULL,
            published_by INTEGER NOT NULL,
            snapshot TEXT NOT NULL)""",
        """CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            match_id INTEGER,
            action TEXT NOT NULL,
            target TEXT,
            details TEXT,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX idx_audit_match ON audit_logs(match_id)",
    ],
    2: [
        # One announcement message per (match, group); lets the bot edit counts in place.
        """CREATE TABLE match_announcements (
            match_id INTEGER NOT NULL REFERENCES matches(id),
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (match_id, chat_id))""",
    ],
}

LATEST = max(MIGRATIONS)


def run_migrations(db: Database) -> int:
    """Apply all pending migrations. Returns the resulting schema version."""
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            log.info("Applying migration %s", version)
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            current = version
    return current
