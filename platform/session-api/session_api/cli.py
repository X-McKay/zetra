"""`session-api`: serve the session interface."""

from __future__ import annotations

from session_api.config import Settings


def main() -> int:
    import uvicorn

    from session_api.app import create_app
    from session_api.jwks import JwksVerifier
    from session_api.real import KubeObjects, StoreReports, TemporalSessions

    try:
        settings = Settings.from_env()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    app = create_app(
        settings,
        JwksVerifier(settings),
        TemporalSessions(settings.tenant),
        KubeObjects(),
        StoreReports(settings.artifact_root),
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0
