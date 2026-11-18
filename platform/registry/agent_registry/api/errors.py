"""The error body and the codes. Every refusal the service makes leaves through `ApiError`.

Body: {"error": {"code": ..., "message": ..., "retryable": ...}}. The codes are the outcome
codes of Kubani's `cli-contract.md`, so an operator command passes them on unchanged.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_registry.rules import Refusal
from agent_registry.store import Conflict, Missing, Unavailable

# code -> (HTTP status, retryable)
CODES: dict[str, tuple[int, bool]] = {
    "invalid-arguments": (400, False),
    "not-signed-in": (401, False),  # no token, or one that does not verify
    "not-found": (404, False),
    # A gate or a rule says no; the message says which. Status 403 instead when the refusal
    # is that a valid token lacks the role a route needs (`ApiError.forbidden`).
    "refused": (409, False),
    "conflict": (409, False),  # the registry and the cluster, or two writes, disagree
    "wrong-path": (409, False),  # the agent is owned by the other path
    "stale-approval": (409, False),  # the approval names something that is no longer so
    "replayed-approval": (409, False),  # this approval id was used before
    "in-recovery": (409, True),  # nothing is written until recovery is left
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


def _answer(error: ApiError) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, error: ApiError) -> JSONResponse:
        return _answer(error)

    @app.exception_handler(Refusal)
    async def _refusal(_: Request, error: Refusal) -> JSONResponse:
        return _answer(ApiError(error.code, error.message))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, error: RequestValidationError) -> JSONResponse:
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        return _answer(ApiError("invalid-arguments", f"{where}: {first.get('msg', 'invalid')}"))

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, error: Conflict) -> JSONResponse:
        return _answer(ApiError("conflict", str(error)))

    @app.exception_handler(Missing)
    async def _missing(_: Request, error: Missing) -> JSONResponse:
        return _answer(ApiError("not-found", str(error)))

    @app.exception_handler(Unavailable)
    async def _unavailable(_: Request, error: Unavailable) -> JSONResponse:
        return _answer(ApiError("unavailable", "the registry database cannot be reached"))

    @app.exception_handler(NotImplementedError)
    async def _unbuilt(_: Request, error: NotImplementedError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "unavailable", "message": "not built yet", "retryable": False}},
            status_code=501,
        )
