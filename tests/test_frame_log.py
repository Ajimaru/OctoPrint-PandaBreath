# coding=utf-8
"""Tests for the daily-rotated JSONL frame logger."""
from __future__ import absolute_import

import datetime
import json
import os

import pytest

from octoprint_pandabreath.frame_log import (
    FrameLog,
    LOG_FILENAME_PREFIX,
    LOG_FILENAME_SUFFIX,
)


def _name_for(day):
    return f"{LOG_FILENAME_PREFIX}{day.isoformat()}{LOG_FILENAME_SUFFIX}"


def _today_name():
    return _name_for(datetime.date.today())


def test_write_creates_dated_file_with_jsonl_entry(tmp_path):
    log = FrameLog(str(tmp_path))
    log.write("rx", {"settings": {"warehouse_temper": 42}}, timestamp=123.0)
    log.close()

    path = tmp_path / _today_name()
    assert path.exists()
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry == {
        "ts": 123.0,
        "dir": "rx",
        "frame": {"settings": {"warehouse_temper": 42}},
    }


def test_write_appends_default_timestamp(tmp_path):
    log = FrameLog(str(tmp_path))
    log.write("tx", "raw-frame")
    log.close()

    path = tmp_path / _today_name()
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["dir"] == "tx"
    assert entry["frame"] == "raw-frame"
    assert isinstance(entry["ts"], float)


def test_multiple_writes_append(tmp_path):
    log = FrameLog(str(tmp_path))
    for i in range(3):
        log.write("rx", {"n": i})
    log.close()

    lines = (tmp_path / _today_name()).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["frame"]["n"] for line in lines] == [0, 1, 2]


def test_close_is_idempotent_and_reopens_on_write(tmp_path):
    log = FrameLog(str(tmp_path))
    log.write("rx", {"a": 1})
    log.close()
    log.close()  # second close must not raise
    log.write("rx", {"a": 2})  # re-opens lazily
    log.close()

    lines = (tmp_path / _today_name()).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_reopens_when_file_deleted_underneath(tmp_path):
    log = FrameLog(str(tmp_path))
    log.write("rx", {"a": 1})
    # Operator deletes the file via the UI while the handle is open.
    (tmp_path / _today_name()).unlink()
    log.write("rx", {"a": 2})
    log.close()

    path = tmp_path / _today_name()
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    # Only the post-deletion write survives in the recreated file.
    assert len(lines) == 1
    assert json.loads(lines[0])["frame"] == {"a": 2}


def test_list_files_returns_sorted_metadata(tmp_path):
    # Drop two valid files plus one unrelated file that must be ignored.
    (tmp_path / "frames_2026-01-01.jsonl").write_text("x\n")
    (tmp_path / "frames_2026-02-01.jsonl").write_text("yy\n")
    (tmp_path / "unrelated.txt").write_text("nope")

    log = FrameLog(str(tmp_path), retention_days=0)
    files = log.list_files()
    names = [name for name, _size, _mtime in files]

    assert "unrelated.txt" not in names
    # Sorted by name descending (newest date first).
    assert names == ["frames_2026-02-01.jsonl", "frames_2026-01-01.jsonl"]
    sizes = {name: size for name, size, _ in files}
    assert sizes["frames_2026-02-01.jsonl"] == 3


def test_list_files_missing_dir_returns_empty(tmp_path):
    log = FrameLog(str(tmp_path), retention_days=0)
    # Remove the directory out from under the logger.
    os.rmdir(tmp_path)
    assert log.list_files() == []


def test_directory_returns_configured_path(tmp_path):
    log = FrameLog(str(tmp_path))
    assert log.directory() == str(tmp_path)


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        ".",
        "..",
        ".hidden",
        "frames_2026-01-01.jsonl/..",
        "../frames_2026-01-01.jsonl",
        "sub/frames_2026-01-01.jsonl",
        "other.txt",
        "frames_not-a-date.jsonl",
        "frames_2026-13-99.jsonl",  # well-formed shape, invalid date
        "frames_.jsonl",
    ],
)
def test_path_for_rejects_bad_names(tmp_path, bad_name):
    log = FrameLog(str(tmp_path))
    assert log.path_for(bad_name) is None


def test_path_for_accepts_valid_name(tmp_path):
    log = FrameLog(str(tmp_path))
    name = "frames_2026-01-01.jsonl"
    resolved = log.path_for(name)
    assert resolved == os.path.join(str(tmp_path), name)


def test_path_for_rejects_symlink(tmp_path):
    # The symlink target lives outside the log dir — the exact traversal
    # the islink guard is meant to block. Use today's date for the name and
    # retention_days=0 so construction-time cleanup never removes the link.
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = FrameLog(str(log_dir), retention_days=0)
    name = _today_name()
    link = log_dir / name
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    assert log.path_for(name) is None


def test_cleanup_removes_files_older_than_retention(tmp_path):
    old_day = datetime.date.today() - datetime.timedelta(days=30)
    recent_day = datetime.date.today() - datetime.timedelta(days=1)
    (tmp_path / _name_for(old_day)).write_text("old\n")
    (tmp_path / _name_for(recent_day)).write_text("recent\n")

    # Constructor runs cleanup with the default 7-day retention.
    FrameLog(str(tmp_path), retention_days=7)

    assert not (tmp_path / _name_for(old_day)).exists()
    assert (tmp_path / _name_for(recent_day)).exists()


def test_cleanup_disabled_when_retention_zero(tmp_path):
    old_day = datetime.date.today() - datetime.timedelta(days=365)
    (tmp_path / _name_for(old_day)).write_text("old\n")

    FrameLog(str(tmp_path), retention_days=0)

    assert (tmp_path / _name_for(old_day)).exists()


def test_negative_retention_clamped_to_zero(tmp_path):
    old_day = datetime.date.today() - datetime.timedelta(days=365)
    (tmp_path / _name_for(old_day)).write_text("old\n")

    # Negative retention is clamped to 0 -> cleanup disabled.
    FrameLog(str(tmp_path), retention_days=-5)

    assert (tmp_path / _name_for(old_day)).exists()


def test_cleanup_ignores_unparseable_names(tmp_path):
    (tmp_path / "frames_garbage.jsonl").write_text("x\n")
    # Must not raise even though the stem is not a date.
    FrameLog(str(tmp_path), retention_days=1)
    assert (tmp_path / "frames_garbage.jsonl").exists()
