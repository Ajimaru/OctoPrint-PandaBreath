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


@pytest.mark.parametrize(
    "flag",
    [
        "is_printing",
        "is_starting",
        "is_pausing",
        "is_paused",
        "is_resuming",
        "is_cancelling",
    ],
)
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


# ---- _printer_is_heating (heater target / hot) ------------------------


class TempPrinter(FakePrinter):
    """Idle printer (no active job) that also reports temperatures."""

    def __init__(self, temps, **flags):
        super().__init__(**flags)
        self._temps = temps

    def get_current_temperatures(self):
        """Return the configured temperature dict."""
        return self._temps


def test_busy_when_bed_target_set():
    """A bed target above 0 blocks drying even with no active job."""
    p = TempPrinter({"bed": {"actual": 25.0, "target": 60.0}})
    assert getattr(_plugin(p), "_printer_is_busy")() is True


def test_busy_when_hotend_target_set():
    """A hotend target above 0 blocks drying."""
    p = TempPrinter({"tool0": {"actual": 25.0, "target": 210.0}})
    assert getattr(_plugin(p), "_printer_is_busy")() is True


def test_busy_when_bed_still_hot_no_target():
    """A bed actual at/above the hot threshold blocks drying (cool-down)."""
    p = TempPrinter({"bed": {"actual": 55.0, "target": 0.0}})
    assert getattr(_plugin(p), "_printer_is_busy")() is True


def test_not_busy_when_cold_and_no_target():
    """Cold heaters with no target do not block drying."""
    p = TempPrinter(
        {
            "bed": {"actual": 24.0, "target": 0.0},
            "tool0": {"actual": 23.0, "target": 0.0},
        }
    )
    assert getattr(_plugin(p), "_printer_is_busy")() is False


def test_chamber_temp_is_ignored():
    """The Panda's own chamber entry must not gate drying."""
    # A hot chamber (the Panda itself) is exactly the drying case — it must
    # not be mistaken for the printer being hot.
    p = TempPrinter({"chamber": {"actual": 55.0, "target": 50.0}})
    assert getattr(_plugin(p), "_printer_is_busy")() is False


def test_heating_probe_fails_open_when_temps_raise():
    """If get_current_temperatures raises, treat as not heating."""

    class RaisingTemps(FakePrinter):
        def get_current_temperatures(self):
            raise RuntimeError("boom")

    assert getattr(_plugin(RaisingTemps()), "_printer_is_busy")() is False


def test_heating_handles_missing_temps():
    """Empty/None temperatures do not block drying."""
    assert getattr(_plugin(TempPrinter({})), "_printer_is_busy")() is False
    assert getattr(_plugin(TempPrinter(None)), "_printer_is_busy")() is False
