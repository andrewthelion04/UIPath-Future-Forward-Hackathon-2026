"""Aplicația FastAPI.

Pornire:  python manage.py api      (sau: python -m app.main --port 8000)
"""

from __future__ import annotations

import argparse
import hmac
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.config import ROOT_DIR, ConfigError, Settings, load_settings
from app.container import Container
from app.routers import COMMON_ROUTERS

# Extensia Chrome și UI-urile locale.
ALLOWED_ORIGINS = r"^(chrome-extension://.*|http://(localhost|127\.0\.0\.1)(:\d+)?)$"


def _configure_logging() -> None:
    logger = logging.getLogger("staypilot")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def requires_api_token(method: str, path: str) -> bool:
    """Ce cere tokenul: tot, în afară de preflight-ul CORS, `/health` și webhook-uri.

    `/health` rămâne deschis ca UI-urile să poată spune „API-ul rulează, dar tokenul lipsește”;
    webhook-urile vin de la PMS, care nu poate trimite antetul nostru și are propriile verificări.
    """
    if method == "OPTIONS" or path == "/health":
        return False
    return not (path == "/webhooks" or path.startswith("/webhooks/"))


def create_app(
    settings: Settings | None = None, *, overrides: dict[str, Any] | None = None
) -> FastAPI:
    """Construiește aplicația. Config invalid -> `ConfigError` (fail-fast la pornire)."""
    _configure_logging()
    settings = settings or load_settings()
    container = Container(settings, overrides=overrides)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.startup()
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(title="StayPilot AI", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    if settings.api_token is not None:
        expected = f"Bearer {settings.api_token.get_secret_value()}".encode()

        @app.middleware("http")
        async def require_api_token(request: Request, call_next: Any) -> Response:
            if requires_api_token(request.method, request.url.path):
                given = request.headers.get("authorization", "").encode()
                if not hmac.compare_digest(given, expected):
                    return JSONResponse(
                        {"detail": "missing or invalid API token"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    # CORS se adaugă ultimul, deci e stratul exterior: și un 401 ajunge cu antetele CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for build_router in COMMON_ROUTERS:
        app.include_router(build_router(container))
    for router in container.routers:  # routerele feature-urilor, descoperite automat
        app.include_router(router)
    return app


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="StayPilot AI API")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-reload", action="store_true", help="disable auto-reload")
    args = parser.parse_args()

    try:
        settings = load_settings()  # validăm înainte să pornim serverul
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    providers = (
        f"pms={settings.pms_provider} llm={settings.llm_provider} "
        f"reviews={settings.reviews_provider} market={settings.market_provider}"
    )
    print(f"StayPilot AI starting ({providers})")
    # Un singur proces: event bus-ul e în proces, iar SQLite e local.
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=not args.no_reload,
        reload_dirs=[str(ROOT_DIR / "app")],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
