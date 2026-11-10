"""Bounded local FFprobe/FFmpeg execution, with no user-controlled command arguments."""

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from youtube_copy.config import Settings
from youtube_copy.worker.contracts import LeaseLostError, LocalMedia, MediaInfo, ProcessingError

DIAGNOSTIC_LIMIT = 64 * 1024


def check_stop(stop: threading.Event, deadline: float) -> None:
    if stop.is_set():
        raise LeaseLostError()
    if time.monotonic() >= deadline:
        raise ProcessingError("processing_timeout")


class MediaProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def execute(
        self, command: list[str], directory: Path, stop: threading.Event, deadline: float
    ) -> bytes:
        check_stop(stop, deadline)
        stdout_path = directory / "stdout"
        file_limit = (
            DIAGNOSTIC_LIMIT
            if command[0] == "/usr/bin/ffprobe"
            else self.settings.worker_max_output_bytes
        )
        wrapper = [
            sys.executable,
            "-m",
            "youtube_copy.worker.exec_media",
            str(os.getpid()),
            str(self.settings.worker_memory_bytes),
            str(self.settings.worker_cpu_seconds),
            str(file_limit),
            *command,
        ]
        # The exec wrapper sets limits without preexec_fn in a threaded process.
        with (
            stdout_path.open("w+b") as stdout,
            subprocess.Popen(
                wrapper,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "PYTHONPATH": str(Path(__file__).parents[2]),
                },
            ) as process,
        ):
            try:
                while process.poll() is None:
                    check_stop(stop, deadline)
                    if shutil.disk_usage(directory).free < self.settings.worker_disk_reserve_bytes:
                        raise ProcessingError("worker_disk_full", retryable=True)
                    if stdout_path.stat().st_size > DIAGNOSTIC_LIMIT:
                        raise ProcessingError("media_diagnostics_limit")
                    stop.wait(0.1)
                check_stop(stop, deadline)
                if process.returncode == 125:
                    raise ProcessingError("media_sandbox_unavailable", retryable=True)
                if process.returncode != 0:
                    raise ProcessingError("invalid_media")
                stdout.seek(0)
                result = stdout.read(DIAGNOSTIC_LIMIT + 1)
                if len(result) > DIAGNOSTIC_LIMIT:
                    raise ProcessingError("media_diagnostics_limit")
                return result
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def probe(
        self, path: Path, stop: threading.Event, deadline: float, *, output: bool = False
    ) -> MediaInfo:
        raw = self.execute(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-threads",
                "2",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,matroska,webm",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
                "-of",
                "json",
                str(path),
            ],
            path.parent,
            stop,
            deadline,
        )
        try:
            data = json.loads(raw)
            streams = data["streams"]
            video = next(
                s
                for s in streams
                if s["codec_type"] == "video"
                and not s.get("disposition", {}).get("attached_pic", 0)
            )
            duration = float(data["format"]["duration"])
            width, height = int(video["width"]), int(video["height"])
            if not math.isfinite(duration) or duration <= 0 or len(streams) > 32:
                raise ValueError()
            if min(width, height) < 2 or width * height > 7680 * 4320:
                raise ValueError()
            if duration > self.settings.worker_max_duration_seconds:
                raise ProcessingError("duration_exceeded")
            if output and (video["codec_name"] != "h264" or width > 1920 or height > 1080):
                raise ValueError()
            if output and any(
                s["codec_type"] == "audio" and s["codec_name"] != "aac" for s in streams
            ):
                raise ValueError()
            return MediaInfo(round(duration * 1000), width, height)
        except (KeyError, ValueError, TypeError, StopIteration) as error:
            raise ProcessingError("invalid_media") from error

    def process(self, original: Path, stop: threading.Event, deadline: float) -> LocalMedia:
        self.probe(original, stop, deadline)
        directory = original.parent
        mp4, thumbnail = directory / "output.mp4", directory / "thumbnail.jpg"
        self.execute(
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                "2",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,matroska,webm",
                "-i",
                str(original),
                "-map",
                "0:V:0",
                "-map",
                "0:a:0?",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-sn",
                "-dn",
                "-vf",
                "scale=w='min(iw,1920)':h='min(ih,1080)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-t",
                str(self.settings.worker_max_duration_seconds + 1),
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(mp4),
            ],
            directory,
            stop,
            deadline,
        )
        info = self.probe(mp4, stop, deadline, output=True)
        self.execute(
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                "2",
                "-filter_threads",
                "1",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov",
                "-i",
                str(mp4),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=w='min(iw,640)':h='min(ih,360)':force_original_aspect_ratio=decrease",
                "-threads",
                "1",
                "-q:v",
                "3",
                "-f",
                "image2",
                "-update",
                "1",
                str(thumbnail),
            ],
            directory,
            stop,
            deadline,
        )
        if not 0 < mp4.stat().st_size <= self.settings.worker_max_output_bytes:
            raise ProcessingError("output_size_exceeded")
        if not 0 < thumbnail.stat().st_size <= 2 * 1024 * 1024:
            raise ProcessingError("thumbnail_invalid")
        return LocalMedia(info, mp4, thumbnail)
