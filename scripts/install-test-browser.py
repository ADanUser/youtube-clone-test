"""Install a pinned Chrome for Testing with H.264/AAC, only in the test image."""

import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "141.0.7390.54"
SHA256 = "5023ec2b8995b74caa5de0e22d5e30f871c3ecce67a6e52d3c9f9dfed423ed01"
URL = (
    f"https://storage.googleapis.com/chrome-for-testing-public/{VERSION}/linux64/chrome-linux64.zip"
)


def install() -> None:
    destination = Path("/opt/chrome")
    with tempfile.TemporaryFile() as archive:
        with urllib.request.urlopen(URL, timeout=180) as response:
            shutil.copyfileobj(response, archive)
        archive.seek(0)
        if hashlib.file_digest(archive, "sha256").hexdigest() != SHA256:
            raise RuntimeError("Chrome for Testing archive checksum mismatch")
        archive.seek(0)
        with zipfile.ZipFile(archive) as package:
            package.extractall(destination)
            for entry in package.infolist():
                mode = (entry.external_attr >> 16) & 0o777
                if mode:
                    (destination / entry.filename).chmod(mode)


if __name__ == "__main__":
    install()
