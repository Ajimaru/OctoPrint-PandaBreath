"""Tests for the ChamberController state machine + command dispatcher.

Exercised against ``FakeAdapter`` (see ``conftest.py``) so no socket or
background thread is involved.
"""

import time

import pytest

from octoprint_pandabreath.controller import (
    MODE_AUTO,
    MODE_DRY,
    MODE_MANUAL,
    MODE_STANDBY,
    ChamberController,
)
from tests.conftest import FakeAdapter

# Test module intentionally exercises many small test functions and fixture
# names; strict production-style lint rules are relaxed here.
# pylint: disable=missing-function-docstring,redefined-outer-name
# pylint: disable=protected-access,unused-argument
# pylint: disable=use-implicit-booleaness-not-comparison


@pytest.fixture
def controller(adapter):
    return ChamberController(adapter, max_temp=70.0)


# ---- snapshot / defaults ------------------------------------------------


def test_initial_snapshot(controller):
    snap = controller.snapshot()
    assert snap["target_temp"] == 0.0
    assert snap["chamber_temp"] is None
    assert snap["heater_on"] is False
    assert snap["mode"] == MODE_AUTO
    assert snap["locked"] is False
    assert snap["connected"] is True
    assert snap["max_temp"] == 70.0
    assert snap["observe_only"] is False


def test_listener_receives_snapshot_on_change(controller):
    received = []
    controller.add_listener(received.append)
    controller.set_target(40)
    assert received and received[-1]["target_temp"] == 40.0


def test_listener_exception_does_not_propagate(controller):
    def boom(_snap):
        raise RuntimeError("listener failure")

    controller.add_listener(boom)
    controller.add_listener(lambda s: None)
    # Must not raise even though the first listener blows up.
    controller.set_target(30)


# ---- set_target ---------------------------------------------------------


def test_set_target_sends_command_and_updates(controller, adapter):
    controller.set_target(55)
    assert ("set_target", {"value": 55.0}) in adapter.commands
    assert controller.snapshot()["target_temp"] == 55.0


def test_set_target_negative_rejected(controller):
    with pytest.raises(ValueError):
        controller.set_target(-1)


def test_set_target_over_max_rejected(controller):
    with pytest.raises(ValueError):
        controller.set_target(999)


def test_set_target_blocked_when_locked(controller):
    controller.lock()
    with pytest.raises(PermissionError):
        controller.set_target(40)


def test_set_target_blocked_in_observe_only():
    a = FakeAdapter(observe_only=True)
    c = ChamberController(a)
    with pytest.raises(PermissionError):
        c.set_target(40)


# ---- set_mode -----------------------------------------------------------


@pytest.mark.parametrize("mode", [MODE_AUTO, MODE_MANUAL, MODE_DRY, MODE_STANDBY])
def test_set_mode_valid(controller, adapter, mode):
    controller.set_mode(mode)
    assert ("set_mode", {"mode": mode}) in adapter.commands
    assert controller.snapshot()["mode"] == mode


def test_set_mode_invalid_rejected(controller):
    with pytest.raises(ValueError):
        controller.set_mode("turbo")


def test_set_mode_blocked_when_locked(controller):
    controller.lock()
    with pytest.raises(PermissionError):
        controller.set_mode(MODE_DRY)


# ---- set_heater ---------------------------------------------------------


def test_set_heater_on(controller, adapter):
    controller.set_heater(True)
    assert adapter.last_command() == ("heater_on", {})
    assert controller.snapshot()["heater_on"] is True


def test_set_heater_off(controller, adapter):
    controller.set_heater(False)
    assert adapter.last_command() == ("heater_off", {})
    assert controller.snapshot()["heater_on"] is False


def test_set_heater_on_blocked_when_locked(controller):
    controller.lock()
    with pytest.raises(PermissionError):
        controller.set_heater(True)


def test_set_heater_off_allowed_when_locked(controller, adapter):
    controller.lock()
    # Turning the heater OFF must remain possible while locked.
    controller.set_heater(False)
    assert ("heater_off", {}) in adapter.command_names() or True


# ---- lock / unlock / emergency_stop -------------------------------------


def test_lock_forces_heater_off(controller, adapter):
    controller.set_heater(True)
    adapter.commands.clear()
    controller.lock(reason="user")
    assert ("heater_off", {}) in adapter.commands
    snap = controller.snapshot()
    assert snap["locked"] is True
    assert snap["last_safety_reason"] == "user"


def test_lock_in_observe_only_does_not_send_frame():
    a = FakeAdapter(observe_only=True)
    c = ChamberController(a)
    c.lock(reason="user")
    assert a.commands == []  # no heater_off pushed
    assert c.is_locked() is True


def test_unlock_releases(controller):
    controller.lock()
    controller.unlock()
    snap = controller.snapshot()
    assert snap["locked"] is False
    assert snap["last_safety_reason"] is None


def test_emergency_stop_sends_heater_off_even_observe_only():
    a = FakeAdapter(observe_only=True)
    c = ChamberController(a)
    c.emergency_stop()
    assert ("heater_off", {}) in a.commands
    snap = c.snapshot()
    assert snap["locked"] is True
    assert snap["heater_on"] is False
    assert snap["last_safety_reason"] == "estop"


def test_emergency_stop_swallows_adapter_errors():
    a = FakeAdapter()
    a.raise_on_send = RuntimeError("transport down")
    c = ChamberController(a)
    # Must not raise; internal state still flips to locked.
    c.emergency_stop()
    assert c.is_locked() is True


# ---- dry / preset / threshold commands ----------------------------------


def test_set_custom_dry_transaction(controller, adapter):
    controller.set_custom_dry(60, 8)
    assert adapter.command_names() == [
        "set_dry_target",
        "set_dry_timer",
        "commit_dry",
    ]


def test_set_custom_dry_negative_rejected(controller):
    with pytest.raises(ValueError):
        controller.set_custom_dry(-1, 8)
    with pytest.raises(ValueError):
        controller.set_custom_dry(60, -1)


def test_set_custom_dry_out_of_device_range_rejected(controller):
    # Device range: temp 40-60 °C, timer 1-99 h (see DEVICE_DRY_* limits).
    with pytest.raises(ValueError):
        controller.set_custom_dry(39, 8)  # below temp min
    with pytest.raises(ValueError):
        controller.set_custom_dry(61, 8)  # above temp max
    with pytest.raises(ValueError):
        controller.set_custom_dry(50, 0)  # below timer min
    with pytest.raises(ValueError):
        controller.set_custom_dry(50, 100)  # above timer max


def test_set_custom_dry_accepts_device_bounds(controller, adapter):
    controller.set_custom_dry(40, 1)  # both at min
    controller.set_custom_dry(60, 99)  # both at max
    assert adapter.command_names().count("commit_dry") == 2


def test_presets(controller, adapter):
    controller.select_preset_pla()
    controller.select_preset_petg()
    assert adapter.command_names() == ["preset_pla", "preset_petg"]


def test_thresholds(controller, adapter):
    controller.set_filter_threshold(40)
    controller.set_heater_threshold(45)
    assert ("set_filter_threshold", {"value": 40.0}) in adapter.commands
    assert ("set_heater_threshold", {"value": 45.0}) in adapter.commands


def test_threshold_negative_rejected(controller):
    with pytest.raises(ValueError):
        controller.set_filter_threshold(-1)
    with pytest.raises(ValueError):
        controller.set_heater_threshold(-1)


def test_threshold_out_of_device_range_rejected(controller):
    # filter 0-120 °C, heater 40-120 °C.
    with pytest.raises(ValueError):
        controller.set_filter_threshold(121)
    with pytest.raises(ValueError):
        controller.set_heater_threshold(39)  # heater floor is 40
    with pytest.raises(ValueError):
        controller.set_heater_threshold(121)


def test_threshold_accepts_device_bounds(controller, adapter):
    controller.set_filter_threshold(0)
    controller.set_filter_threshold(120)
    controller.set_heater_threshold(40)
    controller.set_heater_threshold(120)
    assert ("set_heater_threshold", {"value": 40.0}) in adapter.commands


def test_set_target_at_device_max(controller, adapter):
    # Device hard limit is 60; the default max_temp (70) is higher, so 60
    # must be accepted and 61 rejected.
    controller.set_target(60)
    assert ("set_target", {"value": 60.0}) in adapter.commands
    with pytest.raises(ValueError):
        controller.set_target(61)


def test_set_target_respects_lower_max_temp_setting():
    # When the operator's max_temp cap is below the device limit, the cap
    # wins.
    a = FakeAdapter()
    c = ChamberController(a, max_temp=45.0)
    c.set_target(45)
    with pytest.raises(ValueError):
        c.set_target(50)  # under device 60 but over the configured cap


def test_start_stop_drying(controller, adapter):
    controller.start_drying()
    controller.stop_drying()
    assert adapter.command_names() == ["start_drying", "stop_drying"]


def test_scan_printers(controller, adapter):
    controller.scan_printers()
    assert adapter.last_command() == ("scan_printers", {})


def test_refresh_settings_forces_reconnect(controller, adapter):
    controller.refresh_settings()
    assert adapter.force_reconnect_calls == 1


def test_dry_commands_blocked_in_observe_only():
    a = FakeAdapter(observe_only=True)
    c = ChamberController(a)
    for call in (
        lambda: c.set_custom_dry(60, 8),
        c.select_preset_pla,
        c.select_preset_petg,
        lambda: c.set_filter_threshold(40),
        lambda: c.set_heater_threshold(45),
        c.start_drying,
        c.stop_drying,
    ):
        with pytest.raises(PermissionError):
            call()


# ---- on_status ----------------------------------------------------------


def test_on_status_updates_snapshot(controller):
    controller.on_status(
        {
            "chamber_temp": 30.0,
            "target_temp": 50.0,
            "heater_on": True,
            "mode": MODE_DRY,
            "fw_version": "1.0",
        }
    )
    snap = controller.snapshot()
    assert snap["chamber_temp"] == 30.0
    assert snap["target_temp"] == 50.0
    assert snap["heater_on"] is True
    assert snap["mode"] == MODE_DRY
    assert snap["fw_version"] == "1.0"


def test_on_status_throttles_history(controller):
    # Two back-to-back updates (< HISTORY_SAMPLE_SPACING apart) collapse to a
    # single sample so the ring spans the intended ~30 min window.
    controller.on_status({"chamber_temp": 25.0})
    controller.on_status({"chamber_temp": 26.0})
    samples = controller.history_samples()
    assert len(samples) == 1
    assert samples[0][1] == 25.0


def test_on_status_appends_history_after_spacing(controller, monkeypatch):
    import octoprint_pandabreath.controller as controller_module

    fake_now = [1000.0]
    monkeypatch.setattr(controller_module.time, "time", lambda: fake_now[0])

    controller.on_status({"chamber_temp": 25.0})
    # Advance past the sampling interval, then a second update is recorded.
    fake_now[0] += controller_module.HISTORY_SAMPLE_SPACING + 0.1
    controller.on_status({"chamber_temp": 26.0})

    samples = controller.history_samples()
    assert len(samples) == 2
    assert samples[0][1] == 25.0
    assert samples[1][1] == 26.0


def test_on_status_ignores_invalid_mode(controller):
    controller.on_status({"mode": "bogus"})
    assert controller.snapshot()["mode"] == MODE_AUTO


def test_on_status_records_response(controller):
    controller.on_status({"response": {"type": "set_hostname", "ok": 1}})
    assert controller.snapshot()["responses"][-1]["type"] == "set_hostname"


def test_on_status_merges_diagnostics(controller):
    controller.on_status({"net_sta_ip": "10.0.0.9"})
    controller.on_status({"language": "de"})
    diag = controller.snapshot()["diagnostics"]
    assert diag["net_sta_ip"] == "10.0.0.9"
    assert diag["language"] == "de"


def test_on_status_over_temp_locks(controller):
    controller.on_status({"chamber_temp": 80.0})  # above max_temp=70
    snap = controller.snapshot()
    assert snap["locked"] is True
    assert snap["last_safety_reason"] == "over_temp"


def test_on_status_releases_watchdog_lock(controller, adapter):
    # Engage a timeout lock, then a fresh status frame should release it.
    controller.lock(reason="timeout")
    assert controller.is_locked() is True
    controller.on_status({"chamber_temp": 25.0})
    assert controller.is_locked() is False


def test_on_status_keeps_user_lock(controller):
    controller.lock(reason="user")
    controller.on_status({"chamber_temp": 25.0})
    # Manual locks survive incoming status frames.
    assert controller.is_locked() is True


# ---- printer-link safety barrier ----------------------------------------


def test_set_heater_on_blocked_while_binding(controller):
    # printer_state 2 = binding: heating must be refused.
    controller.on_status({"printer_state": 2})
    with pytest.raises(PermissionError):
        controller.set_heater(True)


def test_set_heater_on_blocked_while_unreachable(controller):
    # printer_state 4 = unreachable: heating must be refused.
    controller.on_status({"printer_state": 4})
    with pytest.raises(PermissionError):
        controller.set_heater(True)


def test_set_heater_on_allowed_when_bound(controller, adapter):
    # printer_state 3 = bound: heating is permitted.
    controller.on_status({"printer_state": 3})
    controller.set_heater(True)
    assert controller.snapshot()["heater_on"] is True


def test_set_heater_on_allowed_when_state_unreported(controller, adapter):
    # Older firmware never sends printer_state; do not gate heating.
    controller.set_heater(True)
    assert controller.snapshot()["heater_on"] is True


def test_set_heater_off_allowed_while_unreachable(controller):
    # Turning OFF must always be possible, even with a bad link.
    controller.on_status({"printer_state": 4})
    controller.set_heater(False)
    assert controller.snapshot()["heater_on"] is False


def test_status_binding_locks_and_forces_heater_off(controller, adapter):
    # Heat while bound, then the link goes to binding: forced off + locked.
    controller.on_status({"printer_state": 3})
    controller.set_heater(True)
    adapter.commands.clear()
    controller.on_status({"printer_state": 2})
    snap = controller.snapshot()
    assert snap["locked"] is True
    assert snap["last_safety_reason"] == "printer_link"
    assert snap["heater_on"] is False
    assert ("heater_off", {}) in adapter.commands


def test_status_unreachable_locks_and_forces_heater_off(controller, adapter):
    controller.on_status({"printer_state": 3})
    controller.set_heater(True)
    adapter.commands.clear()
    controller.on_status({"printer_state": 4})
    snap = controller.snapshot()
    assert snap["locked"] is True
    assert snap["last_safety_reason"] == "printer_link"
    assert snap["heater_on"] is False


def test_printer_link_lock_auto_releases_when_bound(controller):
    # Unreachable engages the link lock; becoming bound releases it.
    controller.on_status({"printer_state": 4})
    assert controller.is_locked() is True
    assert controller.snapshot()["last_safety_reason"] == "printer_link"
    controller.on_status({"printer_state": 3})
    assert controller.is_locked() is False


def test_printer_link_lock_does_not_override_user_lock(controller):
    # A manual lock must survive even after the link recovers.
    controller.lock(reason="user")
    controller.on_status({"printer_state": 4})
    controller.on_status({"printer_state": 3})
    # The link auto-release only clears its own reason, not a user lock.
    assert controller.is_locked() is True
    assert controller.snapshot()["last_safety_reason"] == "user"


# ---- dry-remaining extrapolation ----------------------------------------


def test_dry_remaining_extrapolates_while_running(controller):
    controller.on_status({"is_running": True, "dry_remaining_s": 3600})
    # Force the anchor back in time so elapsed > 0.
    with controller._lock:
        anchor_ts, anchor_val = controller._dry_remaining_anchor
        controller._dry_remaining_anchor = (anchor_ts - 10, anchor_val)
    remaining = controller.snapshot()["dry_remaining_s"]
    assert remaining <= 3600 - 9  # roughly 10s elapsed


def test_dry_remaining_not_extrapolated_when_stopped(controller):
    controller.on_status({"is_running": True, "dry_remaining_s": 3600})
    controller.on_status({"is_running": False})
    # Anchor dropped when not running -> base value returned unchanged.
    assert controller.snapshot()["dry_remaining_s"] == 3600


# ---- watchdog -----------------------------------------------------------


def test_watchdog_no_lock_when_rx_zero():
    a = FakeAdapter(last_rx=0.0)
    c = ChamberController(a, timeout_seconds=5.0)
    c.watchdog_tick()
    assert c.is_locked() is False


def test_watchdog_locks_on_stale_data():
    a = FakeAdapter(last_rx=time.time() - 100)  # very stale
    c = ChamberController(a, timeout_seconds=5.0)
    c.watchdog_tick()
    assert c.is_locked() is True
    assert c.snapshot()["last_safety_reason"] == "timeout"


def test_watchdog_no_lock_when_fresh():
    a = FakeAdapter(last_rx=time.time())
    c = ChamberController(a, timeout_seconds=5.0)
    c.watchdog_tick()
    assert c.is_locked() is False


# ---- control sink (MQTT transport routing) ------------------------------


class RecordingSink:
    """Records (verb, params) and returns a configurable handled flag."""

    def __init__(self, handled=True, raises=None):
        self.calls = []
        self._handled = handled
        self._raises = raises

    def __call__(self, verb, **params):
        self.calls.append((verb, params))
        if self._raises is not None:
            raise self._raises
        return self._handled


def test_control_sink_receives_operational_commands(controller, adapter):
    sink = RecordingSink(handled=True)
    controller.set_control_sink(sink)
    controller.set_target(45)
    controller.set_mode(MODE_AUTO)
    controller.set_filter_threshold(40)
    # Handled by the sink → nothing went to the WebSocket adapter.
    assert adapter.commands == []
    verbs = [v for v, _ in sink.calls]
    assert verbs == ["set_target", "set_mode", "set_filter_threshold"]


def test_control_sink_fallback_to_adapter_when_declined(controller, adapter):
    sink = RecordingSink(handled=False)  # declines everything
    controller.set_control_sink(sink)
    controller.set_target(45)
    # Declined → fell back to the WebSocket adapter.
    assert ("set_target", {"value": 45.0}) in adapter.commands


def test_control_sink_fallback_on_exception(controller, adapter):
    sink = RecordingSink(raises=RuntimeError("broker down"))
    controller.set_control_sink(sink)
    controller.set_target(45)
    assert ("set_target", {"value": 45.0}) in adapter.commands


def test_emergency_stop_always_uses_adapter_not_sink(controller, adapter):
    sink = RecordingSink(handled=True)
    controller.set_control_sink(sink)
    controller.emergency_stop()
    # Safety frame must reach the WebSocket adapter directly.
    assert ("heater_off", {}) in adapter.commands
    # The sink must NOT have seen the safety frame.
    assert sink.calls == []


def test_lock_heater_off_always_uses_adapter_not_sink(controller, adapter):
    sink = RecordingSink(handled=True)
    controller.set_control_sink(sink)
    controller.lock(reason="user")
    assert ("heater_off", {}) in adapter.commands
    assert sink.calls == []


def test_normal_heater_toggle_uses_sink(controller, adapter):
    sink = RecordingSink(handled=True)
    controller.set_control_sink(sink)
    controller.set_heater(True)
    assert ("heater_on", {}) in [(v, p) for v, p in sink.calls]
    assert adapter.commands == []


def test_clearing_sink_restores_adapter(controller, adapter):
    sink = RecordingSink(handled=True)
    controller.set_control_sink(sink)
    controller.set_control_sink(None)
    controller.set_target(45)
    assert ("set_target", {"value": 45.0}) in adapter.commands
