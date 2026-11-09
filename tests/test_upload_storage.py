"""Filesystem errors must never publish an incomplete original."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from youtube_copy.config import Settings
from youtube_copy.infrastructure.uploads import UploadSpool
from youtube_copy.videos.errors import UploadError


def test_short_disk_write_is_rejected(tmp_path: Path) -> None:
    file = Mock()
    file.write.return_value = 2
    settings = Settings(_env_file=None, upload_temp_dir=tmp_path, upload_disk_reserve_bytes=0)
    spool = UploadSpool(file, settings)
    with pytest.raises(UploadError, match="upload_disk_full"):
        spool.write(b"incomplete write")
    assert spool.size == 0
