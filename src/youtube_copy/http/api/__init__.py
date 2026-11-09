"""Versioned JSON API router."""

from fastapi import APIRouter

from youtube_copy.http.api import admin, auth, channels, videos

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(channels.router)
api_router.include_router(admin.router)
api_router.include_router(videos.router)

__all__ = ["api_router"]
