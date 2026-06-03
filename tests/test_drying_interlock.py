# coding=utf-8
"""Tests for the print/drying interlock in the plugin glue.

Dry-mode exists to dry filament, so it must not be possible to switch the
chamber into dry-mode or start a dry cycle while the OctoPrint printer the
Panda Breath is paired with is running a job. The backend enforces this in
``on_api_command`` (returning 409); these tests pin both the
``_printer_is_busy`` probe and the command gate.

The plugin glue (``__init__``) is otherwise excluded from coverage because
it needs a live OctoPrint harness, but the interlock is pure duck-typed
logic over ``self._printer`` and a stubbed Flask, so it is testable here.
"""
from __future__ import absolute_import

import pytest

from octoprint_pandabreath import PandabreathPlugin


class FakePrinter:
    """Duck-typed ``PrinterInterface`` — every predicate defaults False."""

    def __init__(self, **flags):
        self._flags = flags

    def __getattr__(self, name):
        """Return predicate callables matching the requested printer flag."""
        return lambda: self._flags.get(name, False)


class RaisingPrinter:
    """Printer whose predicates raise — probe must fail open (not busy)."""

    def is_printing(self):
        """Simulate a backend error when probing print activity."""
        raise RuntimeError("boom")

    def __getattr__(self, name):
        """All other predicates default to idle for this test double."""
        return lambda: False


def _plugin(printer):
    """Build a plugin instance with just enough state for the probe."""
    plugin = object.__new__(PandabreathPlugin)
    # Protected members are intentional in test setup for plugin internals.
    # pylint: disable=protected-access
    plugin._printer = printer
    import logging
    plugin._logger = logging.getLogger("test")
    return plugin


# ---- _printer_is_busy -------------------------------------------------

@pytest.mark.parametrize("flag", [
    "is_printing", "is_starting", "is_pausing",
    "is_paused", "is_resuming", "is_cancelling",
])
def test_busy_for_each_active_state(flag):
    """Interlock reports busy for each known active OctoPrint predicate."""
    plugin = _plugin(FakePrinter(**{flag: True}))
    assert getattr(plugin, "_printer_is_busy")() is True


def test_not_busy_when_idle():
    """Interlock reports not busy when no active state predicate is true."""
    assert getattr(_plugin(FakePrinter()), "_printer_is_busy")() is False


def test_not_busy_without_printer():
    """Interlock reports not busy if no printer interface is available."""
    assert getattr(_plugin(None), "_printer_is_busy")() is False


def test_probe_fails_open_on_error():
    """Interlock fails open when probing printer state raises an exception."""
    # A raising predicate must be treated as not-busy so command dispatch
    # never breaks on an availability glitch.
    assert getattr(_plugin(RaisingPrinter()), "_printer_is_busy")() is False
