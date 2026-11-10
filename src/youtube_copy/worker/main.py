"""Single-job worker with independent lease heartbeats and safe shutdown."""

import asyncio
import contextlib
import fcntl
import logging
import shutil
import signal
import threading
import time

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from youtube_copy.config import Settings, get_settings
from youtube_copy.worker.contracts import (
    Artifact,
    Lease,
    LeaseLostError,
    MediaInfo,
    ProcessingError,
    Source,
)
from youtube_copy.worker.maintenance import Maintenance
from youtube_copy.worker.media import MediaProcessor, check_stop
from youtube_copy.worker.queue import Queue
from youtube_copy.worker.storage import Storage

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, queue: Queue, storage: Storage, settings: Settings) -> None:
        self.queue, self.storage, self.settings = queue, storage, settings
        self.media = MediaProcessor(settings)
        self.maintenance = Maintenance(queue, storage)
        self.stopping = asyncio.Event()
        self.active_stop: threading.Event | None = None

    def stop(self) -> None:
        self.stopping.set()
        if self.active_stop is not None:
            self.active_stop.set()

    def pipeline(
        self, lease: Lease, source: Source, stop: threading.Event
    ) -> tuple[MediaInfo, list[Artifact]]:
        deadline = time.monotonic() + self.settings.worker_timeout_seconds
        root = self.settings.worker_temp_dir
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        required = source.size_bytes + self.settings.worker_max_output_bytes + 4 * 1024 * 1024
        if shutil.disk_usage(root).free < required + self.settings.worker_disk_reserve_bytes:
            raise ProcessingError("worker_disk_full", retryable=True)
        directory = root / f"job-{lease.token}"
        directory.mkdir(mode=0o700)
        try:
            with (directory / ".lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                original = directory / "original"
                self.storage.download(source, original, stop, deadline)
                output = self.media.process(original, stop, deadline)
                artifacts = [
                    self.storage.upload(
                        output.mp4,
                        f"processed/{source.video_id}/{lease.token}.mp4",
                        "mp4",
                        stop,
                        deadline,
                    ),
                    self.storage.upload(
                        output.thumbnail,
                        f"thumbnails/{source.video_id}/{lease.token}.jpg",
                        "thumbnail",
                        stop,
                        deadline,
                    ),
                ]
                check_stop(stop, deadline)
                return output.info, artifacts
        finally:
            shutil.rmtree(directory)

    async def keep_lease(self, lease: Lease, stop: threading.Event) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            try:
                valid = await self.queue.heartbeat(lease)
                self.touch_heartbeat()
            except (SQLAlchemyError, OSError):
                valid = False
            if not valid or self.stopping.is_set():
                stop.set()
                return

    def touch_heartbeat(self) -> None:
        root = self.settings.worker_temp_dir
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / "heartbeat").touch()

    async def run_once(self) -> bool:
        lease = await self.queue.claim()
        if lease is None:
            return False
        stop = threading.Event()
        self.active_stop = stop
        heartbeat = asyncio.create_task(self.keep_lease(lease, stop))
        pipeline: asyncio.Task | None = None
        logger.info("job_started job_id=%s kind=%s", lease.id, lease.kind)
        try:
            if lease.kind == "transcode":
                source = await self.queue.source(lease)
                if source is None:
                    await self.queue.finish(lease, "superseded")
                else:
                    pipeline = asyncio.create_task(
                        asyncio.to_thread(self.pipeline, lease, source, stop)
                    )
                    info, artifacts = await asyncio.shield(pipeline)
                    if not stop.is_set():
                        published = await self.queue.publish(lease, source, info, artifacts)
                        logger.info("job_completed job_id=%s published=%s", lease.id, published)
            elif lease.kind == "delete":
                await self.maintenance.delete_video(lease)
            elif lease.kind == "cleanup" and lease.payload == {}:
                await self.maintenance.sweep()
                await self.queue.finish(lease)
            else:
                raise ProcessingError("invalid_job_payload")
        except LeaseLostError:
            logger.info("job_lease_lost job_id=%s", lease.id)
        except ValidationError:
            await self.queue.fail(lease, "invalid_job_payload", False)
        except ProcessingError as error:
            await self.queue.fail(lease, error.code, error.retryable)
            logger.info("job_failed job_id=%s code=%s", lease.id, error.code)
        except (BotoCoreError, ClientError):
            await self.queue.fail(lease, "storage_unavailable", True)
        except OSError:
            await self.queue.fail(lease, "worker_io_error", True)
        except SQLAlchemyError:
            logger.warning("job_database_unavailable job_id=%s", lease.id)
        except asyncio.CancelledError:
            stop.set()
            if pipeline is not None:
                with contextlib.suppress(
                    LeaseLostError, ProcessingError, OSError, BotoCoreError, ClientError
                ):
                    await pipeline
            raise
        except Exception:
            await self.queue.fail(lease, "processing_internal_error", True)
            logger.error("job_internal_error job_id=%s", lease.id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.active_stop = None
        return True

    async def serve(self) -> None:
        next_cleanup = 0.0
        while not self.stopping.is_set():
            self.touch_heartbeat()
            try:
                await self.queue.recover()
                if time.monotonic() >= next_cleanup:
                    next_cleanup = time.monotonic() + self.settings.worker_cleanup_interval_seconds
                    try:
                        await self.maintenance.sweep()
                    except (BotoCoreError, ClientError, OSError, SQLAlchemyError):
                        logger.warning("worker_cleanup_unavailable")
                if await self.run_once():
                    continue
            except (SQLAlchemyError, OSError):
                logger.warning("worker_dependency_unavailable")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), self.settings.worker_poll_seconds)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    engine = create_async_engine(
        settings.load_database_url(),
        pool_pre_ping=True,
        connect_args={"timeout": 10, "command_timeout": 30},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = await asyncio.to_thread(Storage, settings)
    worker = Worker(Queue(sessions, settings), storage, settings)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.serve()
    finally:
        await asyncio.to_thread(storage.close)
        await engine.dispose()


def run() -> None:
    asyncio.run(main())
