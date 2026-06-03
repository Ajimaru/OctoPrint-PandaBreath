# coding=utf-8
"""Persistent WebSocket frame logger with daily rotation.

The protocol adapter already keeps a small in-memory ring buffer for the
debug panel; this module persists every frame to disk so an operator can
capture long-running sessions for reverse engineering. One JSONL file per
day, named ``frames_YYYY-MM-DD.jsonl``, rotated lazily on the next write
that crosses the day boundary. Files older than ``retention_days`` are
removed on start and on each rollover.
"""
from __future__ import absolute_import

import datetime
import json
import logging
import os
import threading


LOG_FILENAME_PREFIX = "frames_"
LOG_FILENAME_SUFFIX = ".jsonl"


class FrameLog(object):
    """Daily-rotated JSONL writer for raw protocol frames.

    Safe to call ``write()`` from any thread. ``close()`` releases the
    underlying file handle; subsequent ``write()`` calls re-open lazily.
    """

    def __init__(self, directory, retention_days=7, logger=None):
        self._dir = directory
        self._retention_days = max(0, int(retention_days))
        self._log = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._fp = None
        self._current_day = None  # ``date`` object for the open file
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError:
            self._log.exception(
                "FrameLog: could not create log directory %s", self._dir
            )
        self._cleanup_old_files()

    # ---- public API ------------------------------------------------

    def write(self, direction, frame, timestamp=None):
        """Append a single frame entry to the current day's log file."""
        now = datetime.datetime.now()
        entry = {
            "ts": timestamp if timestamp is not None else now.timestamp(),
            "dir": direction,
            "frame": frame,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self._ensure_open(now.date())
            if self._fp is None:
                return
            try:
                self._fp.write(line)
                self._fp.flush()
            except OSError:
                # Disk full / permission revoked — close the handle so the
                # next call retries cleanly.
                self._log.exception("FrameLog: write failed")
                self._safe_close()

    def list_files(self):
        """Return a list of ``(filename, size_bytes, mtime_epoch)``."""
        try:
            entries = []
            for name in os.listdir(self._dir):
                if not (name.startswith(LOG_FILENAME_PREFIX)
                        and name.endswith(LOG_FILENAME_SUFFIX)):
                    continue
                path = os.path.join(self._dir, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append((name, st.st_size, st.st_mtime))
            entries.sort(key=lambda e: e[0], reverse=True)
            return entries
        except OSError:
            return []

    def directory(self):
        """Return the absolute log directory path."""
        return self._dir

    def path_for(self, filename):
        """Resolve ``filename`` against the log dir, guarding traversal.

        Defence in depth: any of these rejects with ``None``.

        * Empty name, leading dot, path separator anywhere — catches
          ``..``, absolute paths and Windows-style traversal.
        * Filename must match the ``frames_YYYY-MM-DD.jsonl`` shape and
          the date portion must actually parse — refuses anything the
          writer itself wouldn't have produced.
        * Resolved real path must stay inside the log directory
          (``commonpath`` is symlink-/macOS-private-prefix-tolerant
          whereas ``startswith`` is brittle).
        * The resolved entry must be a real file, never a symlink, so a
          symlink dropped into the log dir cannot be used to delete or
          download an arbitrary file.
        """
        if (not filename
                or "/" in filename
                or "\\" in filename
                or os.sep in filename
                or filename in (".", "..")
                or filename.startswith(".")):
            return None
        if not (filename.startswith(LOG_FILENAME_PREFIX)
                and filename.endswith(LOG_FILENAME_SUFFIX)):
            return None
        stem = filename[len(LOG_FILENAME_PREFIX):-len(LOG_FILENAME_SUFFIX)]
        try:
            datetime.date.fromisoformat(stem)
        except ValueError:
            return None
        path = os.path.join(self._dir, filename)
        real_path = os.path.realpath(path)
        real_dir = os.path.realpath(self._dir)
        try:
            if os.path.commonpath([real_path, real_dir]) != real_dir:
                return None
        except ValueError:
            # Different drives on Windows, or empty paths — refuse.
            return None
        # The literal entry in the log dir must not be a symlink — a
        # well-formed name pointing somewhere else is exactly the attack
        # we want to block.
        if os.path.islink(path):
            return None
        return path

    def close(self):
        """Release the underlying file handle. Idempotent."""
        with self._lock:
            self._safe_close()

    # ---- internals -------------------------------------------------

    def _ensure_open(self, today):
        name = f"{LOG_FILENAME_PREFIX}{today.isoformat()}{LOG_FILENAME_SUFFIX}"
        path = os.path.join(self._dir, name)
        # Fast path: same day, file still exists on disk. The os.path.isfile
        # check catches the case where the operator deleted the file via
        # the UI — on Unix the open handle would still write but the data
        # ends up in an unlinked inode, invisible in the directory.
        if (self._fp is not None
                and self._current_day == today
                and os.path.isfile(path)):
            return
        # Day rolled over, first write, or file was deleted out from
        # under us — close any stale handle and reopen.
        self._safe_close()
        self._current_day = today
        try:
            self._fp = open(path, "a", encoding="utf-8")
        except OSError:
            self._log.exception("FrameLog: cannot open %s", path)
            self._fp = None
            return
        self._cleanup_old_files()

    def _safe_close(self):
        if self._fp is None:
            return
        try:
            self._fp.close()
        except OSError:
            self._log.debug("FrameLog: close raised", exc_info=True)
        self._fp = None

    def _cleanup_old_files(self):
        if self._retention_days <= 0:
            return
        cutoff = (
            datetime.date.today()
            - datetime.timedelta(days=self._retention_days)
        )
        try:
            for name in os.listdir(self._dir):
                if not (name.startswith(LOG_FILENAME_PREFIX)
                        and name.endswith(LOG_FILENAME_SUFFIX)):
                    continue
                stem = name[len(LOG_FILENAME_PREFIX):-len(LOG_FILENAME_SUFFIX)]
                try:
                    day = datetime.date.fromisoformat(stem)
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        os.remove(os.path.join(self._dir, name))
                    except OSError:
                        self._log.debug(
                            "FrameLog: could not remove %s", name,
                            exc_info=True,
                        )
        except OSError:
            self._log.debug("FrameLog: cleanup scan failed", exc_info=True)
