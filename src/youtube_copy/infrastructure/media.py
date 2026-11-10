"""Per-request S3 connection, always used and closed in bounded worker-thread calls."""

from typing import Any

import boto3
from botocore.config import Config

from youtube_copy.config import Settings


class MediaStorage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.s3_bucket
        self.body: Any = None
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _connect(self) -> Any:
        settings = self.settings
        access, secret = settings.load_s3_credentials()
        return boto3.client(
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

    def head(self, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=self.bucket, Key=key)

    def open(self, key: str, etag: str, selected: tuple[int, int] | None) -> dict[str, Any]:
        options = {"Range": f"bytes={selected[0]}-{selected[1]}"} if selected else {}
        result = self.client.get_object(Bucket=self.bucket, Key=key, IfMatch=etag, **options)
        self.body = result["Body"]
        return result

    def read(self, amount: int) -> bytes:
        return self.body.read(amount)

    def close(self) -> None:
        try:
            if self.body is not None:
                self.body.close()
        finally:
            if self._client is not None:
                self._client.close()
