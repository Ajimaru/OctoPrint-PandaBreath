# coding=utf-8
"""Tests for the debug-panel gate on the on-disk frame log.

The debug panel is the master switch for all debug-only behaviour. Disk
capture must run only when the panel is enabled *and* the dedicated
``frame_log_enabled`` toggle is on — so flipping the panel off stops
persistence regardless of that toggle. ``_refresh_frame_log`` is pure
duck-typed logic over ``self._settings`` and ``get_plugin_data_folder``,
so it is testable here without a live OctoPrint harness.
"""
from __future__ import absolute_import

import logging
from typing import Any, cast

import pytest

from octoprint_pandabreath import PandabreathPlugin


class FakeSettings:
    """Minimal stand-in for OctoPrint's settings access."""

    def __init__(self, **bools):
        self._bools = bools

    def get_boolean(self, path):
        """Return boolean settings values by key path."""
        return bool(self._bools.get(path[0], False))

    def get(self, path):
        """Return raw settings values by key path."""
        return self._bools.get(path[0])


def _plugin(tmp_path, **bools):
    """Build a minimally initialized plugin instance for gate testing."""
    plugin = cast(Any, object.__new__(PandabreathPlugin))
    # Protected members are intentionally manipulated in tests.
    # pylint: disable=protected-access
    plugin._logger = logging.getLogger("test")
    plugin._frame_log = None
    plugin._settings = FakeSettings(**bools)
    plugin.get_plugin_data_folder = lambda: str(tmp_path)
    return plugin


@pytest.mark.parametrize("debug,persist,expect_open", [
    (True, True, True),     # both on -> log open
    (True, False, False),   # persist off -> closed
    (False, True, False),   # debug off gates persist -> closed
    (False, False, False),  # both off -> closed
])
def test_frame_log_gate(tmp_path, debug, persist, expect_open):
    """Frame log opens only when debug panel and persistence toggle are on."""
    plugin = _plugin(
        tmp_path,
        debug_panel_enabled=debug,
        frame_log_enabled=persist,
        frame_log_retention_days=7,
    )
    getattr(plugin, "_refresh_frame_log")()
    frame_log = getattr(plugin, "_frame_log")
    assert (frame_log is not None) is expect_open
    if frame_log is not None:
        frame_log.close()


def test_disabling_debug_closes_open_log(tmp_path):
    """Disabling the debug panel closes an already-open frame log."""
    # Open with both on, then turn the debug panel off -> must close.
    plugin = _plugin(
        tmp_path,
        debug_panel_enabled=True,
        frame_log_enabled=True,
        frame_log_retention_days=7,
    )
    getattr(plugin, "_refresh_frame_log")()
    assert getattr(plugin, "_frame_log") is not None

    setattr(plugin, "_settings", FakeSettings(
        debug_panel_enabled=False,
        frame_log_enabled=True,
        frame_log_retention_days=7,
    ))
    getattr(plugin, "_refresh_frame_log")()
    assert getattr(plugin, "_frame_log") is None
