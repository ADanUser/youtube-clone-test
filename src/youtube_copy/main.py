"""ASGI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from youtube_copy.accounts.rate_limit import SlidingWindowRateLimiter
from youtube_copy.config import Settings, get_settings
from youtube_copy.http.api import api_router
from youtube_copy.infrastructure.database.session import dispose_database_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP application without connecting to external services."""
    application_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        yield
        await dispose_database_engine()

    application = FastAPI(title="YouTube copy", debug=application_settings.debug, lifespan=lifespan)
    application.state.settings = application_settings
    application.state.auth_rate_limiter = SlidingWindowRateLimiter()
    application.include_router(api_router)

    @application.get("/healthz", tags=["operations"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()


def run() -> None:
    """Run the development ASGI server."""
    uvicorn.run("youtube_copy.main:app", host="127.0.0.1", port=8000, reload=False)
