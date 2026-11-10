"""ASGI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from youtube_copy.accounts.rate_limit import SlidingWindowRateLimiter
from youtube_copy.config import Settings, get_settings
from youtube_copy.http.api import api_router
from youtube_copy.http.html.playback import router as playback_page_router
from youtube_copy.http.html.uploads import router as upload_page_router
from youtube_copy.infrastructure.database.session import dispose_database_engine
from youtube_copy.infrastructure.uploads import OriginalStorage, UploadSlots
from youtube_copy.videos.errors import UploadError


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
    application.state.upload_rate_limiter = SlidingWindowRateLimiter()
    application.state.upload_slots = UploadSlots(application_settings)
    application.state.original_storage = OriginalStorage(application_settings)
    application.include_router(api_router)
    application.include_router(upload_page_router)
    application.include_router(playback_page_router)
    application.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "http/html/static"), name="static"
    )

    @application.exception_handler(UploadError)
    async def upload_error_handler(_request: Request, error: UploadError) -> JSONResponse:
        return JSONResponse(
            {"detail": error.code},
            status_code=error.status_code,
            headers={"Cache-Control": "private, no-store"},
        )

    @application.get("/healthz", tags=["operations"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()


def run() -> None:
    """Run the development ASGI server."""
    uvicorn.run("youtube_copy.main:app", host="127.0.0.1", port=8000, reload=False)
