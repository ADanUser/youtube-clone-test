"""Idempotently bootstrap the single-node Garage cluster used by Compose."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config

from youtube_copy.config import Settings

LOGGER = logging.getLogger(__name__)
GARAGE_BINARY = "/usr/local/bin/garage"
EXPECTED_GARAGE_VERSION = "v2.5.0"
APPLICATION_UID = 10001
APPLICATION_GID = 10001


class BootstrapError(RuntimeError):
    """Raised when existing Garage state is incompatible with the contract."""


def run_garage(*arguments: str) -> str:
    """Run the pinned Garage CLI while keeping captured secrets out of logs."""
    result = subprocess.run(
        [GARAGE_BINARY, *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "RUST_LOG": "error"},
    )
    return result.stdout.strip()


def garage_json(endpoint: str, payload: Mapping[str, Any] | None = None) -> Any:
    """Invoke the local administration API through authenticated Garage RPC."""
    arguments = ["json-api", endpoint]
    if payload is not None:
        arguments.append(json.dumps(payload, separators=(",", ":")))
    return json.loads(run_garage(*arguments))


def expected_role(settings: Settings, node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "zone": settings.garage_layout_zone,
        "capacity": settings.garage_layout_capacity_bytes,
        "tags": [],
    }


def role_matches(role: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Compare the stable fields returned by the Garage v2 admin API."""
    return all(role.get(field) == expected[field] for field in ("id", "zone", "capacity", "tags"))


def wait_for_node(settings: Settings) -> tuple[str, int]:
    deadline = time.monotonic() + settings.garage_init_timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = garage_json("GetClusterStatus")
            nodes = status.get("nodes", [])
            if len(nodes) != 1:
                raise BootstrapError("expected exactly one Garage node")
            node = nodes[0]
            if node.get("garageVersion") != EXPECTED_GARAGE_VERSION:
                raise BootstrapError("Garage CLI and server versions do not match")
            if node.get("isUp") is True:
                return str(node["id"]), int(status["layoutVersion"])
        except (BootstrapError, OSError, subprocess.SubprocessError, ValueError) as error:
            last_error = error
        time.sleep(settings.garage_init_poll_seconds)
    raise BootstrapError(
        "Garage did not become available before the bootstrap timeout"
    ) from last_error


def ensure_layout(settings: Settings, node_id: str) -> None:
    layout = garage_json("GetClusterLayout")
    expected = expected_role(settings, node_id)
    roles = layout.get("roles", [])
    staged = layout.get("stagedRoleChanges", [])
    staged_parameters = layout.get("stagedParameters")

    if staged_parameters is not None:
        raise BootstrapError("unexpected staged layout parameters")
    if len(roles) > 1 or any(role.get("id") != node_id for role in roles):
        raise BootstrapError("layout contains an unexpected Garage node")

    if roles:
        if not role_matches(roles[0], expected):
            raise BootstrapError("existing Garage layout is incompatible")
        if staged:
            raise BootstrapError("unexpected staged changes on an initialized layout")
        return

    if not staged:
        LOGGER.info("Staging the single-node Garage layout")
        run_garage(
            "layout",
            "assign",
            "--zone",
            settings.garage_layout_zone,
            "--capacity",
            str(settings.garage_layout_capacity_bytes),
            node_id,
        )
        layout = garage_json("GetClusterLayout")
        staged = layout.get("stagedRoleChanges", [])

    if len(staged) != 1 or not role_matches(staged[0], expected):
        raise BootstrapError("staged Garage layout is incompatible")

    next_version = int(layout["version"]) + 1
    LOGGER.info("Applying Garage layout version %s", next_version)
    run_garage("layout", "apply", "--version", str(next_version))
    applied = garage_json("GetClusterLayout")
    if int(applied["version"]) != next_version:
        raise BootstrapError("Garage layout version did not advance as expected")
    applied_roles = applied.get("roles", [])
    if len(applied_roles) != 1 or not role_matches(applied_roles[0], expected):
        raise BootstrapError("applied Garage layout does not match the requested role")


def find_named(
    items: Sequence[Mapping[str, Any]], name_field: str, name: str
) -> Mapping[str, Any] | None:
    matches = [item for item in items if item.get(name_field) == name]
    if len(matches) > 1:
        raise BootstrapError(f"multiple Garage resources use the name {name!r}")
    return matches[0] if matches else None


def ensure_bucket(settings: Settings) -> Mapping[str, Any]:
    buckets = garage_json("ListBuckets")
    matches = [
        bucket for bucket in buckets if settings.s3_bucket in bucket.get("globalAliases", [])
    ]
    if len(matches) > 1:
        raise BootstrapError("multiple buckets expose the configured global alias")
    if not matches:
        LOGGER.info("Creating the private Garage bucket")
        run_garage("bucket", "create", settings.s3_bucket)
    bucket = garage_json("GetBucketInfo", {"globalAlias": settings.s3_bucket})
    if bucket.get("websiteAccess") is not False:
        raise BootstrapError("the application bucket must not have website access")
    return bucket


def ensure_application_key(settings: Settings) -> Mapping[str, Any]:
    keys = garage_json("ListKeys")
    key = find_named(keys, "name", settings.garage_app_key_name)
    if key is None:
        LOGGER.info("Creating the Garage application key")
        run_garage("key", "create", settings.garage_app_key_name)
    key_info = garage_json("GetKeyInfo", {"search": settings.garage_app_key_name})
    if key_info.get("name") != settings.garage_app_key_name or key_info.get("expired") is not False:
        raise BootstrapError("Garage application key is invalid")
    if any(key_info.get("permissions", {}).values()):
        raise BootstrapError("Garage application key has unexpected global permissions")
    return key_info


def ensure_bucket_permissions(
    settings: Settings, bucket: Mapping[str, Any], key: Mapping[str, Any]
) -> None:
    key_id = key["accessKeyId"]
    entries = [entry for entry in bucket.get("keys", []) if entry.get("accessKeyId") == key_id]
    if len(entries) > 1:
        raise BootstrapError("duplicate application-key permissions on the bucket")
    expected = {"read": True, "write": True, "owner": False}
    if not entries:
        LOGGER.info("Granting read/write access to the application bucket")
        run_garage(
            "bucket",
            "allow",
            "--read",
            "--write",
            settings.s3_bucket,
            "--key",
            str(key_id),
        )
        bucket = garage_json("GetBucketInfo", {"globalAlias": settings.s3_bucket})
        entries = [entry for entry in bucket.get("keys", []) if entry.get("accessKeyId") == key_id]
    if len(entries) != 1 or entries[0].get("permissions") != expected:
        raise BootstrapError("Garage application key must have read/write and no owner permission")


def atomic_secret_write(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o640)
        os.write(descriptor, f"{value}\n".encode())
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chown(temporary, APPLICATION_UID, APPLICATION_GID)
        temporary.replace(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def persist_credentials(settings: Settings) -> tuple[str, str]:
    if settings.s3_access_key_file is None or settings.s3_secret_key_file is None:
        raise BootstrapError("file-based S3 credential paths are required for garage-init")
    access_key = run_garage("key", "info", "--key-id", settings.garage_app_key_name)
    secret_key = run_garage("key", "info", "--secret-key", settings.garage_app_key_name)
    if not access_key or not secret_key:
        raise BootstrapError("Garage did not export complete application credentials")
    atomic_secret_write(settings.s3_access_key_file, access_key)
    atomic_secret_write(settings.s3_secret_key_file, secret_key)
    return access_key, secret_key


def verify_s3(settings: Settings, access_key: str, secret_key: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=str(settings.s3_endpoint_url),
        region_name=settings.s3_region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    object_key = f".garage-init/probe-{uuid.uuid4()}"
    payload = b"garage-range-probe"
    client.head_bucket(Bucket=settings.s3_bucket)
    try:
        client.put_object(Bucket=settings.s3_bucket, Key=object_key, Body=payload)
        full = client.get_object(Bucket=settings.s3_bucket, Key=object_key)["Body"].read()
        ranged = client.get_object(Bucket=settings.s3_bucket, Key=object_key, Range="bytes=1-5")[
            "Body"
        ].read()
        if full != payload or ranged != payload[1:6]:
            raise BootstrapError("Garage S3 verification returned unexpected object bytes")
    finally:
        client.delete_object(Bucket=settings.s3_bucket, Key=object_key)


def bootstrap(settings: Settings) -> None:
    if settings.s3_access_key_file is None:
        raise BootstrapError("garage-init requires a shared credential directory")
    lock_path = settings.s3_access_key_file.parent / ".init.lock"
    lock_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        LOGGER.info("Waiting for the Garage node")
        node_id, _ = wait_for_node(settings)
        ensure_layout(settings, node_id)
        bucket = ensure_bucket(settings)
        key = ensure_application_key(settings)
        ensure_bucket_permissions(settings, bucket, key)
        access_key, secret_key = persist_credentials(settings)
        LOGGER.info("Verifying Garage through the S3 API")
        verify_s3(settings, access_key, secret_key)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        bootstrap(Settings())
    except (BootstrapError, OSError, subprocess.SubprocessError, ValueError) as error:
        LOGGER.error("Garage bootstrap failed: %s", error)
        raise SystemExit(1) from error
    LOGGER.info("Garage bootstrap completed")


if __name__ == "__main__":
    main()
