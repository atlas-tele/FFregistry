"""Authorization. Admins are identified ONLY by immutable numeric Telegram user ID."""
from __future__ import annotations

from typing import Iterable, Optional

from database import Database
from database import repositories as repo
from .errors import PermissionDenied

PRIVATE = "private"
GROUP_TYPES = ("group", "supergroup")


class PermissionService:
    def __init__(self, db: Database, owner_id: int, env_admin_ids: Iterable[int] = ()) -> None:
        self.db = db
        self.owner_id = owner_id
        self.env_admin_ids = frozenset(env_admin_ids)

    def is_owner(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id == self.owner_id

    def is_admin(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        if self.is_owner(user_id) or user_id in self.env_admin_ids:
            return True
        with self.db.read() as conn:
            return user_id in repo.list_admin_ids(conn)

    def require_admin_context(self, chat_type: Optional[str], user_id: Optional[int]) -> None:
        """Admin operations: private chat AND authorized user. Checked on every command/callback."""
        if chat_type != PRIVATE:
            raise PermissionDenied("Admin commands only work in a private chat with the bot.", "not_private")
        if not self.is_admin(user_id):
            raise PermissionDenied("You are not authorized to use this bot's admin features.", "not_admin")

    def require_owner_context(self, chat_type: Optional[str], user_id: Optional[int]) -> None:
        self.require_admin_context(chat_type, user_id)
        if not self.is_owner(user_id):
            raise PermissionDenied("Only the owner can do this.", "not_owner")

    def list_all_admins(self) -> dict:
        with self.db.read() as conn:
            db_admins = repo.list_admin_ids(conn)
        return {"owner": self.owner_id, "env": sorted(self.env_admin_ids), "db": db_admins}

    def add_admin(self, actor_id: int, target_id: int) -> None:
        if not self.is_owner(actor_id):
            raise PermissionDenied("Only the owner can manage admins.", "not_owner")
        if target_id <= 0:
            raise PermissionDenied("Invalid user ID.", "invalid")
        with self.db.transaction() as conn:
            repo.add_admin(conn, target_id, actor_id)
            repo.add_audit(conn, actor_id, None, "admin_add", str(target_id))

    def remove_admin(self, actor_id: int, target_id: int) -> None:
        if not self.is_owner(actor_id):
            raise PermissionDenied("Only the owner can manage admins.", "not_owner")
        if target_id == self.owner_id or target_id in self.env_admin_ids:
            raise PermissionDenied("Owner/ADMIN_IDS entries are managed via environment variables.", "protected")
        with self.db.transaction() as conn:
            if not repo.remove_admin(conn, target_id):
                raise PermissionDenied("That user is not a bot-managed admin.", "invalid")
            repo.add_audit(conn, actor_id, None, "admin_remove", str(target_id))

    def is_group_authorized(self, chat_id: int) -> bool:
        with self.db.read() as conn:
            g = repo.get_group(conn, chat_id)
            return bool(g and g["authorized"])
