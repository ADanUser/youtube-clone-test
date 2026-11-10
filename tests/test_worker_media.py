"""Actual FFmpeg output and fail-closed Linux decoder boundary."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from youtube_copy.config import Settings
from youtube_copy.worker.contracts import LeaseLostError, ProcessingError
from youtube_copy.worker.media import MediaProcessor

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/usr/bin/ffmpeg").exists(),
    reason="run media tests in the Linux FFmpeg test image",
)


def make_video(path: Path, *, audio: bool = True, duration: int = 1) -> None:
    command = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x180:r=10",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    command += ["-t", str(duration), "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run([*command, str(path)], check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("audio", [True, False])
def test_transcode_and_thumbnail(tmp_path: Path, audio: bool) -> None:
    path = tmp_path / "source.mkv"
    make_video(path, audio=audio)
    media = MediaProcessor(Settings(_env_file=None))
    result = media.process(path, threading.Event(), time.monotonic() + 30)
    assert result.info.width == 320 and result.info.height == 180
    assert 900 <= result.info.duration_ms <= 1500
    content = result.mp4.read_bytes()
    assert content.index(b"moov") < content.index(b"mdat")
    assert result.thumbnail.read_bytes().startswith(b"\xff\xd8\xff")
    assert result.mp4.stat().st_size > 0


@pytest.mark.parametrize("kind", ["bad", "audio", "playlist", "long"])
def test_invalid_media_is_terminal(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "original.mp4"
    if kind == "long":
        make_video(path, duration=3)
    elif kind == "audio":
        subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                "1",
                "-c:a",
                "aac",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
    else:
        path.write_bytes(
            b"not a video" if kind == "bad" else b"#EXTM3U\nhttp://127.0.0.1/private\n"
        )
    media = MediaProcessor(Settings(_env_file=None, worker_max_duration_seconds=2))
    with pytest.raises(ProcessingError) as error:
        media.process(path, threading.Event(), time.monotonic() + 30)
    assert error.value.code == ("duration_exceeded" if kind == "long" else "invalid_media")
    assert not error.value.retryable
    assert not (tmp_path / "output.mp4").exists()


def test_stop_and_deadline(tmp_path: Path) -> None:
    media = MediaProcessor(Settings(_env_file=None))
    stop = threading.Event()
    stop.set()
    with pytest.raises(LeaseLostError):
        media.execute(["/usr/bin/ffprobe", "-version"], tmp_path, stop, time.monotonic() + 10)
    with pytest.raises(ProcessingError, match="processing_timeout"):
        media.execute(["/usr/bin/ffprobe", "-version"], tmp_path, threading.Event(), 0)


def test_decoder_network_sandbox_denies_sockets() -> None:
    # Isolated child: never restrict pytest's database/S3 sockets.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from youtube_copy.worker.exec_media import restrict_network; "
            "import socket; restrict_network(); socket.socket()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


def test_running_process_is_killed_on_deadline(tmp_path: Path) -> None:
    from unittest.mock import patch

    media = MediaProcessor(Settings(_env_file=None))
    processes = []
    start = subprocess.Popen

    def record(*args, **kwargs):
        process = start(*args, **kwargs)
        processes.append(process)
        return process

    with (
        patch("youtube_copy.worker.media.subprocess.Popen", record),
        pytest.raises(ProcessingError, match="processing_timeout"),
    ):
        media.execute(
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "color=s=32x32:r=1",
                "-t",
                "60",
                "-f",
                "null",
                "-",
            ],
            tmp_path,
            threading.Event(),
            time.monotonic() + 0.5,
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None
