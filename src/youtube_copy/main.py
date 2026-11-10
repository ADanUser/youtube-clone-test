"""ASGI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from youtube_copy.accounts.rate_limit import SlidingWindowRateLimiter
from youtube_copy.config import Settings, get_settings
from youtube_copy.http.api import api_router
from youtube_copy.http.html.pages import router as pages_router
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
    application.include_router(pages_router)
    application.include_router(upload_page_router)
    application.include_router(playback_page_router)
    application.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "http/html/static"), name="static"
    )

    templates = Jinja2Templates(directory=Path(__file__).parent / "http/html/templates")

    def html_error(request: Request, status_code: int) -> Response:
        messages = {
            401: ("Требуется вход", "Войдите в аккаунт, чтобы открыть эту страницу."),
            403: ("Доступ запрещён", "У вас нет доступа к запрошенной странице."),  # noqa: RUF001
            404: ("Страница не найдена", "Возможно, адрес неверен или материал недоступен."),
        }
        title, message = messages.get(
            status_code, ("Что-то пошло не так", "Попробуйте обновить страницу чуть позже.")
        )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": status_code, "title": title, "message": message, "auth": None},
            status_code=status_code,
            headers={"Cache-Control": "private, no-store"},
        )

    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, error: HTTPException) -> Response:
        if not request.url.path.startswith("/api/"):
            return html_error(request, error.status_code)
        return await http_exception_handler(request, error)

    @application.exception_handler(UploadError)
    async def upload_error_handler(request: Request, error: UploadError) -> Response:
        if not request.url.path.startswith("/api/"):
            return html_error(request, error.status_code)
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
