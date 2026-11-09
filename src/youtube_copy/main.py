"""ASGI application entry point."""

import uvicorn
from fastapi import FastAPI

from youtube_copy.config import get_settings


def create_app() -> FastAPI:
    """Create the HTTP application without connecting to external services."""
    settings = get_settings()
    application = FastAPI(title="YouTube copy", debug=settings.debug)

    @application.get("/healthz", tags=["operations"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()


def run() -> None:
    """Run the development ASGI server."""
    uvicorn.run("youtube_copy.main:app", host="127.0.0.1", port=8000, reload=False)
