"""Background worker entry point reserved for processing jobs."""

import logging
import signal
import threading

from youtube_copy.config import get_settings


def run() -> None:
    """Keep the worker process alive until the processing loop is implemented."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger(__name__)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Worker scaffold is ready")
    stop.wait()
    logger.info("Worker scaffold stopped")
