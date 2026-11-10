"""Private S3 originals and output generations; bounded I/O and safe object names."""

import hashlib
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from youtube_copy.config import Settings
from youtube_copy.worker.contracts import Artifact, ProcessingError, Source
from youtube_copy.worker.media import check_stop


def managed_key(key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    parts = key.split("/")
    if len(parts) != 3 or parts[0] not in ("originals", "processed", "thumbnails"):
        return None
    suffix = {"originals": "", "processed": ".mp4", "thumbnails": ".jpg"}[parts[0]]
    generation = parts[2]
    if suffix:
        if not generation.endswith(suffix):
            return None
        generation = generation[: -len(suffix)]
    try:
        video_id, generation_id = uuid.UUID(parts[1]), uuid.UUID(generation)
    except ValueError:
        return None
    if str(video_id) != parts[1] or str(generation_id) != generation:
        return None
    return video_id, generation_id


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        access, secret = settings.load_s3_credentials()
        self.client = boto3.client(
            "s3",
            endpoint_url=str(settings.s3_endpoint_url),
            region_name=settings.s3_region,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
        self.bucket = settings.s3_bucket

    def close(self) -> None:
        self.client.close()

    def download(self, source: Source, path: Path, stop: threading.Event, deadline: float) -> None:
        if source.size_bytes > self.settings.upload_max_bytes or source.size_bytes <= 0:
            raise ProcessingError("original_size_invalid")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=source.object_key)
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                raise ProcessingError("original_missing") from error
            raise
        body = response["Body"]
        digest, count = hashlib.sha256(), 0
        try:
            if response["ContentLength"] != source.size_bytes:
                raise ProcessingError("original_size_invalid")
            with path.open("wb", buffering=0) as file:
                while chunk := body.read(256 * 1024):
                    check_stop(stop, deadline)
                    count += len(chunk)
                    if count > source.size_bytes:
                        raise ProcessingError("original_size_invalid")
                    if shutil.disk_usage(
                        path.parent
                    ).free < self.settings.worker_disk_reserve_bytes + len(chunk):
                        raise ProcessingError("worker_disk_full", retryable=True)
                    if file.write(chunk) != len(chunk):
                        raise ProcessingError("worker_disk_full", retryable=True)
                    digest.update(chunk)
        finally:
            body.close()
        if count != source.size_bytes or digest.hexdigest() != source.sha256:
            raise ProcessingError("original_checksum_mismatch")

    def upload(
        self,
        path: Path,
        key: str,
        kind: Literal["mp4", "thumbnail"],
        stop: threading.Event,
        deadline: float,
    ) -> Artifact:
        check_stop(stop, deadline)
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(256 * 1024):
                check_stop(stop, deadline)
                digest.update(chunk)
        mime = "video/mp4" if kind == "mp4" else "image/jpeg"

        def progress(_bytes: int) -> None:
            check_stop(stop, deadline)

        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": mime, "Metadata": {"sha256": digest.hexdigest()}},
            Callback=progress,
            Config=TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                use_threads=False,
            ),
        )
        check_stop(stop, deadline)
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        size = path.stat().st_size
        if (
            head["ContentLength"] != size
            or head.get("Metadata", {}).get("sha256") != digest.hexdigest()
        ):
            raise ProcessingError("output_verification_failed", retryable=True)
        return Artifact(kind, key, size, digest.hexdigest(), mime)

    def delete(self, key: str) -> None:
        if managed_key(key) is None:
            raise ProcessingError("invalid_delete_key")
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def objects(self, prefix: str, token: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            arguments["ContinuationToken"] = token
        return self.client.list_objects_v2(**arguments)

    def multiparts(
        self, key_marker: str | None = None, upload_marker: str | None = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"Bucket": self.bucket, "MaxUploads": 1000}
        if key_marker:
            arguments["KeyMarker"] = key_marker
        if upload_marker:
            arguments["UploadIdMarker"] = upload_marker
        return self.client.list_multipart_uploads(**arguments)

    def abort(self, key: str, upload_id: str) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchUpload":
                raise
