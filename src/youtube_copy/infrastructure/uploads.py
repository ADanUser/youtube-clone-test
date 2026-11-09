"""Bounded temporary storage and synchronous S3 adapter, called in worker threads."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
from typing import BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from youtube_copy.config import Settings
from youtube_copy.videos.errors import UploadError

WRITE_CHUNK_BYTES = 256 * 1024


class UploadSpool:
    def __init__(self, file: BinaryIO, settings: Settings) -> None:
        self.file = file
        self.settings = settings
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes) -> None:
        if self.size + len(chunk) > self.settings.upload_max_bytes:
            raise UploadError(413, "upload_too_large")
        # Account for external disk consumers as well as our admission reservation.
        free = shutil.disk_usage(self.settings.upload_temp_dir).free
        if free < self.settings.upload_disk_reserve_bytes + len(chunk):
            raise UploadError(507, "upload_disk_full")
        if self.file.write(chunk) != len(chunk):
            raise UploadError(507, "upload_disk_full")
        self.digest.update(chunk)
        self.size += len(chunk)


class UploadSlots:
    """Conservative whole-file reservations for the single web process in Compose."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> UploadSpool:
        with self._lock:
            if self._active >= self.settings.upload_concurrency:
                raise UploadError(503, "upload_capacity_exhausted")
            directory = self.settings.upload_temp_dir
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            required = (
                self._active + 1
            ) * self.settings.upload_max_bytes + self.settings.upload_disk_reserve_bytes
            if shutil.disk_usage(directory).free < required:
                raise UploadError(507, "upload_disk_full")
            # Anonymous/unlinked immediately: process death releases the disk as well.
            file = tempfile.TemporaryFile(  # noqa: SIM115 - ownership passes to release()
                mode="w+b", buffering=0, dir=directory
            )
            self._active += 1
            return UploadSpool(file, self.settings)

    def release(self, spool: UploadSpool) -> None:
        try:
            spool.file.close()
        finally:
            with self._lock:
                self._active -= 1


class OriginalStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def put(self, key: str, spool: UploadSpool) -> None:
        access_key, secret_key = self.settings.load_s3_credentials()
        # Explicit credentials prevent fallback to metadata services.
        client = boto3.client(
            "s3",
            endpoint_url=str(self.settings.s3_endpoint_url),
            region_name=self.settings.s3_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
        try:
            spool.file.seek(0)
            client.upload_fileobj(
                spool.file,
                self.settings.s3_bucket,
                key,
                ExtraArgs={"ContentType": "application/octet-stream"},
                Config=TransferConfig(
                    multipart_threshold=8 * 1024 * 1024,
                    multipart_chunksize=8 * 1024 * 1024,
                    use_threads=False,
                ),
            )
        finally:
            client.close()
