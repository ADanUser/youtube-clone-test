"""Small upload screen; authorization and business rules remain in shared services."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
async def upload_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={"max_bytes": request.app.state.settings.upload_max_bytes},
        headers={"Cache-Control": "private, no-store"},
    )
