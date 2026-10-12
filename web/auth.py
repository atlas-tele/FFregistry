"""Small environment-backed admin authentication helpers."""
from __future__ import annotations
import hashlib, hmac, os, secrets
from typing import Optional


def _password_env(username: str) -> Optional[str]:
    # Username values are deliberately restricted to avoid surprising env-key mapping.
    if not username or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in username):
        return None
    for i in (1, 2):
        if hmac.compare_digest(username, (os.getenv(f"WEB_ADMIN{i}_USERNAME") or "").strip()):
            value = os.getenv(f"WEB_ADMIN{i}_PASSWORD") or ""
            return value if len(value) >= 12 else None
    return None


def verify_credentials(username: str, password: str) -> bool:
    expected = _password_env(username.strip())
    if expected is None:
        # Constant-ish work for unknown usernames.
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"ffregistry-login", 120_000)
        return False
    # PBKDF2 protects comparisons from timing leaks and avoids plain equality on passwords.
    salt = hashlib.sha256((username.strip().lower() + ":ffregistry").encode()).digest()
    a = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    b = hashlib.pbkdf2_hmac("sha256", expected.encode(), salt, 120_000)
    return hmac.compare_digest(a, b)


def configured_admins() -> list[dict[str, str]]:
    result = []
    for i in (1, 2):
        username = (os.getenv(f"WEB_ADMIN{i}_USERNAME") or "").strip()
        password = os.getenv(f"WEB_ADMIN{i}_PASSWORD") or ""
        if username and len(password) >= 12:
            result.append({"username": username})
    return result
