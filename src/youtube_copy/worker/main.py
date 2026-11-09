"""Background worker entry point reserved for processing jobs."""

import logging

from youtube_copy.config import get_settings


def run() -> None:
    """Validate worker configuration without starting job processing yet."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger(__name__).info("Worker scaffold is configured")
