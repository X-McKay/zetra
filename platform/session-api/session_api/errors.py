"""The error body and the codes, as the registry's (`contract/sessions.md` section 3)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# code -> (HTTP status, retryable)
CODES: dict[str, tuple[int, bool]] = {
    "invalid-arguments": (400, False),
    "not-signed-in": (401, False),
    "not-found": (404, False),
    "refused": (409, False),  # status 403 instead when the token may not do this at all
    "wrong-path": (409, False),
    "conflict": (409, False),
    "unavailable": (503, True),
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or CODES[code][0]

    @classmethod
    def forbidden(cls, message: str) -> ApiError:
        return cls("refused", message, status=403)

    def body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": CODES[self.code][1],
            }
        }


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(error.body(), status_code=error.status)
