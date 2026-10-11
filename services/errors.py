"""Domain errors. `message` is always safe to show to end users."""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class PermissionDenied(ServiceError):
    pass
