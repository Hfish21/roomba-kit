"""The kit's single error catalogue (contracts/errors.md)."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    ROBOT_NOT_FOUND = "robot_not_found"
    AMBIGUOUS_NAME = "ambiguous_name"
    ROOM_NOT_FOUND = "room_not_found"
    NOT_SUPPORTED = "not_supported"
    UNSUPPORTED_MODEL = "unsupported_model"
    ROBOT_OFFLINE = "robot_offline"
    CONNECTION_HELD_ELSEWHERE = "connection_held_elsewhere"
    AUTH_FAILED = "auth_failed"
    UNCONFIRMED = "unconfirmed"
    NOT_APPLIED = "not_applied"
    MAP_CHANGED = "map_changed"
    NO_MAP = "no_map"
    CLOUD_UNAVAILABLE = "cloud_unavailable"
    PASSWORD_UNAVAILABLE = "password_unavailable"  # noqa: S105 - an error code, not a secret
    TIMEOUT = "timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL = "internal"


# HTTP status per code. `service_unavailable` is CLI/stdio-MCP only, so it has no HTTP status.
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.ROBOT_NOT_FOUND: 404,
    ErrorCode.AMBIGUOUS_NAME: 409,
    ErrorCode.ROOM_NOT_FOUND: 404,
    ErrorCode.NOT_SUPPORTED: 422,
    ErrorCode.UNSUPPORTED_MODEL: 422,
    ErrorCode.ROBOT_OFFLINE: 503,
    ErrorCode.CONNECTION_HELD_ELSEWHERE: 503,
    ErrorCode.AUTH_FAILED: 502,
    ErrorCode.UNCONFIRMED: 202,
    ErrorCode.NOT_APPLIED: 409,
    ErrorCode.MAP_CHANGED: 409,
    ErrorCode.NO_MAP: 409,
    ErrorCode.CLOUD_UNAVAILABLE: 503,
    ErrorCode.PASSWORD_UNAVAILABLE: 409,
    ErrorCode.TIMEOUT: 408,
    ErrorCode.INTERNAL: 500,
}

CLI_EXIT: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 2,
    ErrorCode.UNAUTHORIZED: 77,
    ErrorCode.ROBOT_NOT_FOUND: 4,
    ErrorCode.AMBIGUOUS_NAME: 4,
    ErrorCode.ROOM_NOT_FOUND: 4,
    ErrorCode.NOT_SUPPORTED: 5,
    ErrorCode.UNSUPPORTED_MODEL: 5,
    ErrorCode.ROBOT_OFFLINE: 6,
    ErrorCode.CONNECTION_HELD_ELSEWHERE: 6,
    ErrorCode.AUTH_FAILED: 7,
    ErrorCode.UNCONFIRMED: 3,
    ErrorCode.NOT_APPLIED: 3,
    ErrorCode.MAP_CHANGED: 8,
    ErrorCode.NO_MAP: 8,
    ErrorCode.CLOUD_UNAVAILABLE: 9,
    ErrorCode.PASSWORD_UNAVAILABLE: 10,
    ErrorCode.TIMEOUT: 11,
    ErrorCode.SERVICE_UNAVAILABLE: 69,
    ErrorCode.INTERNAL: 70,
}


class KitError(Exception):
    """The only exception type that crosses the core boundary. Messages never carry secrets."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        robot_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.robot_id = robot_id
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        """The `error` object of the wire envelope `{"error": {...}}`."""
        body: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.robot_id is not None:
            body["robot_id"] = self.robot_id
        if self.details is not None:
            body["details"] = self.details
        return body

    def __repr__(self) -> str:
        return f"KitError({self.code.value!r}, {self.message!r})"
