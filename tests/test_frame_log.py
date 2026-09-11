"""Tests for the daily-rotated JSONL frame logger."""

import datetime
import json
import os

import pytest

from octoprint_pandabreath.frame_log import (
    LOG_FILENAME_PREFIX,
    LOG_FILENAME_SUFFIX,
    FrameLog,
)


def _name_for(day):
    """Build the canonical frame-log filename for a given date."""
    return f"{LOG_FILENAME_PREFIX}{day.isoformat()}{LOG_FILENAME_SUFFIX}"


def _today_name():
    """Return today's frame-log filename."""
    return _name_for(datetime.date.today())


def test_write_creates_dated_file_with_jsonl_entry(tmp_path):
    """A write creates today's file and stores a JSONL record."""
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
    """Writes without explicit timestamp include an auto-generated float ts."""
    log = FrameLog(str(tmp_path))
    log.write("tx", "raw-frame")
    log.close()

    path = tmp_path / _today_name()
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["dir"] == "tx"
    assert entry["frame"] == "raw-frame"
    assert isinstance(entry["ts"], float)


def test_multiple_writes_append(tmp_path):
    """Multiple writes append additional JSONL lines to the same file."""
    log = FrameLog(str(tmp_path))
    for i in range(3):
        log.write("rx", {"n": i})
    log.close()

    lines = (tmp_path / _today_name()).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["frame"]["n"] for line in lines] == [0, 1, 2]


def test_close_is_idempotent_and_reopens_on_write(tmp_path):
    """Closing twice is safe and a subsequent write reopens lazily."""
    log = FrameLog(str(tmp_path))
    log.write("rx", {"a": 1})
    log.close()
    log.close()  # second close must not raise
    log.write("rx", {"a": 2})  # re-opens lazily
    log.close()

    lines = (tmp_path / _today_name()).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_reopens_when_file_deleted_underneath(tmp_path):
    """The logger recreates today's file when it is deleted externally."""
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
    """list_files returns only valid log files sorted newest-first."""
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
    """list_files returns empty when the backing directory no longer exists."""
    log = FrameLog(str(tmp_path), retention_days=0)
    # Remove the directory out from under the logger.
    os.rmdir(tmp_path)
    assert not log.list_files()


def test_directory_returns_configured_path(tmp_path):
    """directory returns the exact configured storage path."""
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
    """path_for rejects unsafe or malformed filenames."""
    log = FrameLog(str(tmp_path))
    assert log.path_for(bad_name) is None


def test_path_for_accepts_valid_name(tmp_path):
    """path_for resolves a valid log filename inside the log directory."""
    log = FrameLog(str(tmp_path))
    name = "frames_2026-01-01.jsonl"
    resolved = log.path_for(name)
    assert resolved == os.path.join(str(tmp_path), name)


def test_path_for_rejects_symlink(tmp_path):
    """path_for rejects symlinked files to prevent path traversal via links."""
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
    """Constructor cleanup removes files older than retention_days."""
    old_day = datetime.date.today() - datetime.timedelta(days=30)
    recent_day = datetime.date.today() - datetime.timedelta(days=1)
    (tmp_path / _name_for(old_day)).write_text("old\n")
    (tmp_path / _name_for(recent_day)).write_text("recent\n")

    # Constructor runs cleanup with the default 7-day retention.
    FrameLog(str(tmp_path), retention_days=7)

    assert not (tmp_path / _name_for(old_day)).exists()
    assert (tmp_path / _name_for(recent_day)).exists()


def test_cleanup_disabled_when_retention_zero(tmp_path):
    """Retention value 0 disables cleanup of historical files."""
    old_day = datetime.date.today() - datetime.timedelta(days=365)
    (tmp_path / _name_for(old_day)).write_text("old\n")

    FrameLog(str(tmp_path), retention_days=0)

    assert (tmp_path / _name_for(old_day)).exists()


def test_negative_retention_clamped_to_zero(tmp_path):
    """Negative retention values are clamped so cleanup stays disabled."""
    old_day = datetime.date.today() - datetime.timedelta(days=365)
    (tmp_path / _name_for(old_day)).write_text("old\n")

    # Negative retention is clamped to 0 -> cleanup disabled.
    FrameLog(str(tmp_path), retention_days=-5)

    assert (tmp_path / _name_for(old_day)).exists()


def test_cleanup_ignores_unparseable_names(tmp_path):
    """Cleanup skips invalidly named files without raising errors."""
    (tmp_path / "frames_garbage.jsonl").write_text("x\n")
    # Must not raise even though the stem is not a date.
    FrameLog(str(tmp_path), retention_days=1)
    assert (tmp_path / "frames_garbage.jsonl").exists()


def test_mkdir_oserror_is_swallowed(tmp_path, monkeypatch):
    """An OSError from makedirs is logged but does not propagate."""
    monkeypatch.setattr(
        os, "makedirs", lambda *a, **kw: (_ for _ in ()).throw(OSError("no space"))
    )
    # Must not raise; the logger logs and moves on.
    FrameLog(str(tmp_path / "nonexistent"))


def test_write_with_open_failure_does_not_raise(tmp_path, monkeypatch):
    """If _ensure_open leaves _fp=None the write is silently skipped."""
    log = FrameLog(str(tmp_path))
    # Force _ensure_open to fail by making open() raise.
    real_open = open

    def _fail_open(path, *a, **kw):
        if "frames_" in str(path):
            raise OSError("permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fail_open)
    log.write("rx", {"x": 1})  # must not raise


def test_write_oserror_on_flush_closes_handle(tmp_path, monkeypatch):
    """An OSError during write/flush closes the file handle."""
    log = FrameLog(str(tmp_path))
    # Prime the handle by writing once successfully.
    log.write("rx", {"x": 1})
    assert log._fp is not None

    # Now make fp.write raise.
    class _FailFP:
        def write(self, _data):
            raise OSError("disk full")

        def flush(self):
            pass

        def close(self):
            pass

    log._fp = _FailFP()
    log.write("rx", {"y": 2})  # must not raise
    assert log._fp is None


def test_list_files_stat_error_skips_entry(tmp_path, monkeypatch):
    """A stat error for an individual file skips it without crashing."""
    log = FrameLog(str(tmp_path))
    log.write("rx", {"x": 1})
    log.close()

    real_stat = os.stat

    def _fail_stat(path, *a, **kw):
        if "frames_" in str(path):
            raise OSError("stat failed")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "stat", _fail_stat)
    result = log.list_files()
    assert result == []


def test_safe_close_oserror_swallowed(tmp_path):
    """An OSError during _safe_close is swallowed."""
    log = FrameLog(str(tmp_path))
    log.write("rx", {"x": 1})

    class _FailClose:
        def write(self, _d):
            pass

        def flush(self):
            pass

        def close(self):
            raise OSError("close error")

    log._fp = _FailClose()
    log.close()  # must not raise
    assert log._fp is None


def test_path_for_commonpath_value_error(tmp_path, monkeypatch):
    """ValueError from commonpath (e.g. different drives) returns None."""
    log = FrameLog(str(tmp_path), retention_days=0)
    name = _today_name()
    (tmp_path / name).write_text("data\n")

    monkeypatch.setattr(
        os.path,
        "commonpath",
        lambda _paths: (_ for _ in ()).throw(ValueError("different drives")),
    )
    assert log.path_for(name) is None


def test_cleanup_remove_oserror_swallowed(tmp_path, monkeypatch):
    """An OSError removing an old file during cleanup is swallowed."""
    old_day = datetime.date.today() - datetime.timedelta(days=30)
    (tmp_path / _name_for(old_day)).write_text("old\n")

    real_remove = os.remove

    def _fail_remove(path):
        if "frames_" in str(path):
            raise OSError("permission denied")
        real_remove(path)

    monkeypatch.setattr(os, "remove", _fail_remove)
    # Must not raise even though removal failed.
    FrameLog(str(tmp_path), retention_days=7)
