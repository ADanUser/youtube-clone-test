"""Versioned JSON API router."""

from fastapi import APIRouter

from youtube_copy.http.api import admin, auth, channels, moderation, playback, social, videos

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(channels.router)
api_router.include_router(admin.router)
api_router.include_router(videos.router)
api_router.include_router(playback.router)
api_router.include_router(social.router)
api_router.include_router(moderation.router)

__all__ = ["api_router"]
