"""Chrome drives real forms, JavaScript, PostgreSQL, Garage and FFmpeg."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import Browser, Page, async_playwright, expect
from sqlalchemy import update

from test_worker import Context
from test_worker import worker_context as worker_context
from test_worker_media import make_video
from youtube_copy.infrastructure.database.models import User
from youtube_copy.infrastructure.database.session import get_db_session
from youtube_copy.main import create_app

pytestmark = [pytest.mark.postgres, pytest.mark.browser]
type BrowserCase = tuple[Context, Page, str, Browser]


@pytest.fixture
async def browser_case(worker_context: Context, tmp_path: Path) -> AsyncIterator[BrowserCase]:
    if os.environ.get("YOUTUBE_RUN_BROWSER_TESTS") != "1":
        pytest.skip("set YOUTUBE_RUN_BROWSER_TESTS=1 in the Linux browser test image")
    ctx = worker_context
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = ctx.settings.model_copy(update={"public_origin": origin})
    app = create_app(settings)

    async def session():
        async with ctx.sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = session
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/opt/chrome/chrome-linux64/chrome",
                env={
                    **os.environ,
                    "XDG_CONFIG_HOME": str(tmp_path / "chrome-config"),
                    "XDG_CACHE_HOME": str(tmp_path / "chrome-cache"),
                },
            )
            context = await browser.new_context(base_url=origin)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                yield ctx, page, origin, browser
                assert errors == [], errors
            finally:
                await context.close()
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def register_ui(page: Page, name: str) -> None:
    await page.goto("/register")
    await page.get_by_label("Email", exact=True).fill(name + "@example.com")
    await page.get_by_label("Имя пользователя").fill(name)
    await page.get_by_label("Пароль", exact=True).fill("browser test password 123")
    await page.get_by_role("button", name="Зарегистрироваться").click()
    await page.wait_for_url("**/")
    await expect(page.locator("body")).to_have_css("background-color", "rgb(15, 15, 15)")


async def upload_ui(ctx: Context, page: Page, tmp_path: Path, visibility: str = "public") -> str:
    movie = tmp_path / "browser.mp4"
    await asyncio.to_thread(make_video, movie, duration=8)
    await page.goto("/upload")
    await page.get_by_label("Название", exact=True).fill(
        "Browser video <img src=x onerror=alert(1)>"
    )
    await page.get_by_label("Видимость").select_option(visibility)
    await page.locator('input[type="file"]').set_input_files(movie)
    async with page.expect_response(
        lambda r: r.url.endswith("/upload") and r.status == 202
    ) as uploaded:
        await page.get_by_role("button", name="Загрузить", exact=True).click()
    await expect(page.locator("#progress")).to_have_attribute("value", "100")
    await expect(page.locator("#status")).to_contain_text("Ожидаем завершения обработки")
    video_id = (await uploaded.value).url.rsplit("/", 2)[1]
    ctx.videos.append(uuid.UUID(video_id))
    assert await ctx.status(uuid.UUID(video_id)) == ("processing", None, "queued", 0)
    assert await ctx.worker.run_once()
    await page.get_by_role("link", name="Смотреть видео").click(timeout=30_000)
    await expect(page.locator("h1")).to_have_text("Browser video <img src=x onerror=alert(1)>")
    assert await page.locator("h1 img").count() == 0
    return video_id


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
async def test_registration_upload_play_seek_social_and_logout(
    browser_case: BrowserCase, tmp_path: Path, width: int
) -> None:
    ctx, page, _, _ = browser_case
    await page.set_viewport_size({"width": width, "height": 900})
    name = "browser-" + uuid.uuid4().hex[:12]
    await register_ui(page, name)
    video_id = await upload_ui(ctx, page, tmp_path)
    await page.wait_for_function(
        "document.querySelector('video').readyState >= 1 || document.querySelector('video').error"
    )
    media = await page.locator("video").evaluate(
        "v => ({state: v.readyState, error: v.error?.message, code: v.error?.code})"
    )
    assert media.get("error") is None, media
    await asyncio.wait_for(
        page.locator("video").evaluate("v => { v.muted = true; return v.play(); }"), 15
    )
    await page.wait_for_function("document.querySelector('video').currentTime > 0.1")
    await page.locator("video").evaluate("v => { v.pause(); v.currentTime = 5; }")
    await page.wait_for_function(
        "!document.querySelector('video').seeking && "
        "document.querySelector('video').currentTime >= 5"
    )
    assert await page.locator("video").evaluate("v => v.error === null && v.videoWidth > 0")
    await page.locator("[data-like]").click()
    await expect(page.locator("[data-like-count]")).to_have_text("1")
    payload = '<img src=x onerror="window.xssExecuted=true">'
    await page.get_by_placeholder("Оставьте комментарий").fill(payload)
    async with (
        page.expect_response(
            lambda r: r.url.endswith("/comments") and r.request.method == "POST"
        ) as comment_response,
        page.expect_navigation(wait_until="domcontentloaded") as refreshed,
    ):
        await page.locator("[data-comment-form] button").click()
    assert (await comment_response.value).status == 201
    assert (await refreshed.value).status == 200
    await expect(page.locator("article").filter(has_text=payload)).to_be_visible()
    assert await page.evaluate("window.xssExecuted === undefined")
    await page.reload()
    await expect(page.locator("[data-like-count]")).to_have_text("1")
    await page.goto("/playlists")
    await page.get_by_placeholder("Название плейлиста").fill("Browser playlist")
    await page.get_by_role("button", name="Создать", exact=True).click()
    await expect(page.locator("h1")).to_have_text("Browser playlist")
    playlist_url = page.url
    await page.goto(f"/watch/{video_id}")
    await page.locator("[data-playlist-open]").click()
    await page.get_by_role("button", name="Browser playlist", exact=True).click()
    await expect(page.locator("#toast")).to_have_text("Видео сохранено")
    await page.goto(playlist_url)
    await expect(page.locator("[data-video-order]")).to_have_count(1)
    await page.locator("[data-remove-video]").click()
    await expect(page.locator("[data-video-order]")).to_have_count(0)
    await page.locator("[data-logout]").click()
    await page.wait_for_url("**/")
    assert (await page.request.get("/api/v1/auth/me")).status == 401
    await page.goto("/login")
    await page.get_by_label("Email или имя пользователя").fill(name)
    await page.get_by_label("Пароль", exact=True).fill("browser test password 123")
    await page.get_by_role("button", name="Войти", exact=True).click()
    await page.wait_for_url("**/")
    await page.goto(f"/watch/{video_id}")
    async with page.expect_navigation(wait_until="domcontentloaded") as refreshed:
        await page.locator("[data-delete-comment]").click()
    assert (await refreshed.value).status == 200
    await expect(page.locator("article")).to_have_count(0)


async def test_browser_private_grant_revocation_and_csrf(
    browser_case: BrowserCase, tmp_path: Path
) -> None:
    ctx, owner, origin, browser = browser_case
    owner_name = "owner-" + uuid.uuid4().hex[:12]
    await register_ui(owner, owner_name)
    video_id = await upload_ui(ctx, owner, tmp_path, "private")
    viewer_context = await browser.new_context(base_url=origin)
    try:
        viewer = await viewer_context.new_page()
        viewer_errors: list[str] = []
        viewer.on("pageerror", lambda error: viewer_errors.append(str(error)))
        await register_ui(viewer, "viewer-" + uuid.uuid4().hex[:12])
        viewer_id = (await (await viewer.request.get("/api/v1/auth/me")).json())["id"]
        await viewer.goto(f"/channel/{owner_name}")
        await viewer.locator("[data-subscribe]").click()
        await expect(viewer.locator("[data-subscriber-count]")).to_have_text("1")
        await viewer.reload()
        await expect(viewer.locator("[data-subscribe]")).to_have_text("Вы подписаны")
        assert (await viewer.goto(f"/watch/{video_id}")).status == 404
        grant = f"/api/v1/videos/{video_id}/grants/{viewer_id}"
        assert await owner.evaluate("url => mutate(url, 'PUT').then(r => r.status)", grant) == 204
        await viewer.goto(f"/watch/{video_id}")
        await asyncio.wait_for(
            viewer.locator("video").evaluate("v => { v.muted = true; return v.play(); }"), 15
        )
        await viewer.wait_for_function("document.querySelector('video').currentTime > 0.1")
        # Browser fetch without CSRF must not create a like.
        assert (
            await viewer.evaluate(
                "url => fetch(url, {method:'PUT'}).then(r => r.status)",
                f"/api/v1/videos/{video_id}/like",
            )
            == 403
        )
        await viewer.reload()
        await expect(viewer.locator("[data-like-count]")).to_have_text("0")
        assert (
            await owner.evaluate("url => mutate(url, 'DELETE').then(r => r.status)", grant) == 204
        )
        for suffix in ("stream", "thumbnail"):
            response = await viewer.request.get(
                f"/api/v1/videos/{video_id}/{suffix}", headers={"Range": "bytes=1-10"}
            )
            assert response.status == 404
            assert "etag" not in response.headers
        assert (await viewer.goto(f"/watch/{video_id}")).status == 404
        assert viewer_errors == [], viewer_errors
    finally:
        await viewer_context.close()


async def test_browser_report_and_hide(browser_case: BrowserCase, tmp_path: Path) -> None:
    ctx, page, _, _ = browser_case
    name = "moderator-" + uuid.uuid4().hex[:12]
    await register_ui(page, name)
    video_id = await upload_ui(ctx, page, tmp_path)
    await page.locator("[data-report-open]").click()
    await page.get_by_placeholder("Опишите нарушение").fill("Browser report")
    await page.locator("[data-report-form] button").click()
    await expect(page.locator("#toast")).to_have_text("Жалоба отправлена")
    async with ctx.sessions.begin() as db:
        await db.execute(
            update(User).where(User.username_normalized == name).values(role="moderator")
        )
    await page.goto("/moderation")
    row = page.locator("[data-report-row]").filter(has_text="Browser report")
    await expect(row).to_be_visible()
    await row.get_by_placeholder("Причина решения").fill("Confirmed by browser test")
    await row.get_by_role("button", name="Скрыть", exact=True).click()
    await expect(row).to_have_count(0)
    assert (await page.goto(f"/watch/{video_id}")).status == 404
