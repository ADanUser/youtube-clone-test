"""Small upload screen; authorization and business rules remain in shared services."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from youtube_copy.accounts.dependencies import get_optional_auth
from youtube_copy.accounts.service import AuthContext

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
Viewer = Annotated[AuthContext | None, Depends(get_optional_auth)]


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
async def upload_page(request: Request, auth: Viewer) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={"max_bytes": request.app.state.settings.upload_max_bytes, "auth": auth},
        headers={"Cache-Control": "private, no-store"},
    )
