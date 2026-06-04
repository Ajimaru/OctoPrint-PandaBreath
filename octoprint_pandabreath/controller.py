#
# Portions of this file are derived from MIT-licensed upstream work.
# Per the MIT License, the original copyright notices below are retained.
#
#   Upstream: BIQU-Panda-Breath-Mod (file: Panda.py)
#     URL:       https://github.com/jeng37/BIQU-Panda-Breath-Mod
#     License:   MIT
#     Copyright: Copyright (c) [2026] [Jeng]
#
#   Upstream: chamber_control (file: chamber_control.py)
#     URL:       https://github.com/bula87/chamber_control
#     License:   MIT
#     Copyright: Copyright (c) 2026 Wojciech K
#
# Full upstream LICENSE texts are reproduced under licenses/ in the
# repository root (BIQU-Panda-Breath-Mod-LICENSE, chamber_control-LICENSE).
"""
ChamberController — UI-side state + command dispatcher for the Panda
Breath chamber heater.

The mode names (auto/manual/dry/standby) and their integer encoding
(1/2/3/0) are taken from the upstream projects referenced in the file
header; the integer mapping itself lives in protocol.py.

Earlier revisions of this module ran a local hysteresis regulator that
toggled the heater based on target/chamber temperatures. That was
removed once we confirmed the Panda firmware regulates the chamber on
its own (using ``hotbedtemp`` / ``filtertemp`` thresholds in auto-mode):
two regulators fighting each other re-enabled the heater after the user
clicked OFF. The controller now only forwards explicit user actions and
maintains a UI snapshot — the device is the single source of truth for
when the heater is on.
"""

import collections
import logging
import threading
import time

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_DRY = "dry"
MODE_STANDBY = "standby"
VALID_MODES = (MODE_AUTO, MODE_MANUAL, MODE_DRY, MODE_STANDBY)

# Device-enforced input ranges, reverse-engineered from the V1.0.4 Home
# Assistant MQTT discovery configs (and confirmed by WebUI clamping). The
# firmware rejects / clamps out-of-range values on its own; we mirror the
# limits here so the plugin gives the same feedback before sending a frame.
# (target_temp's upper bound is additionally capped by the configurable
# ``max_temp`` safety setting, applied in ``set_target``.)
DEVICE_TARGET_MIN = 0.0
DEVICE_TARGET_MAX = 60.0
DEVICE_FILTER_THRESHOLD_MIN = 0.0
DEVICE_FILTER_THRESHOLD_MAX = 120.0
DEVICE_HEATER_THRESHOLD_MIN = 40.0
DEVICE_HEATER_THRESHOLD_MAX = 120.0
DEVICE_DRY_TARGET_MIN = 40.0
DEVICE_DRY_TARGET_MAX = 60.0
# The discovery config declares custom_timer min=1, but the device itself
# accepted and held 0 in testing. We use 1 as the lower bound to match the
# device's own HA contract; 0 is a documented edge case.
DEVICE_DRY_TIMER_MIN = 1
DEVICE_DRY_TIMER_MAX = 99

# Temperature-history ring buffer size. At a 5 s adapter poll cadence this
# covers ~30 minutes; samples older than that are silently dropped.
HISTORY_MAX_SAMPLES = 360


class ChamberController:  # pylint: disable=too-many-instance-attributes
    """Pass-through controller for the chamber heater.

    Sits between :class:`PandaProtocolAdapter` (the transport) and the
    plugin's UI/API layer. Forwards user actions (set target, set mode,
    heater on/off, dry-mode commands) to the device, mirrors the
    device's status frames into a UI-friendly snapshot, and owns the
    safety-lock state. Does *not* run a local heater regulator — the
    Panda firmware regulates on its own.

    The controller intentionally holds a flat set of state attributes
    (target, mode, lock, last reason, adapter, listeners); grouping them
    into helper structs would obscure the lock invariants and add
    ceremony without improving readability.
    """

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        adapter,
        max_temp=70.0,
        timeout_seconds=15.0,
        logger=None,
    ):
        self._adapter = adapter
        self._max_temp = float(max_temp)
        self._timeout = float(timeout_seconds)
        self._log = logger or logging.getLogger(__name__)
        # Optional alternate transport for *operational* commands. When set
        # (the plugin installs the MQTT bridge here), set_target/set_mode/
        # dry/threshold/heater-on-off frames go through it instead of the
        # WebSocket adapter, avoiding the adapter's reconnect-after-write
        # (the WS does not ACK TX frames). The callable takes
        # ``(verb, **params)`` and returns True if it handled the send;
        # any falsey return or absence falls back to the WebSocket.
        #
        # SAFETY: this sink is *never* used for emergency_stop or the
        # lock-driven heater_off — those always go straight to the
        # WebSocket adapter so they cannot depend on broker availability.
        self._control_sink = None

        self._lock = threading.Lock()
        self._target = 0.0
        self._chamber_temp = None
        self._heater_on = False
        self._mode = MODE_AUTO
        self._locked = False
        self._last_safety_reason = None
        self._listeners = []  # type: list
        # Firmware-reported extras. None means "not seen yet".
        self._fw_version = None
        self._dry_target = None
        self._dry_timer_hours = None
        self._dry_remaining_s = None
        # Anchor for live-extrapolating the dry-mode countdown between
        # the device's sparse remaining_seconds updates. The device only
        # ships the value in big snapshot frames (post-reconnect), so
        # between two such snapshots ``_dry_remaining_s`` would stay
        # stale. We remember the last (monotonic_ts, remaining_seconds)
        # pair while a cycle is running and subtract the elapsed wall
        # time in ``snapshot()`` to keep the value fresh for the UI.
        self._dry_remaining_anchor = None
        self._bed_temp_limit = None
        self._filter_threshold = None
        self._is_running = None
        self._printer_type = None
        self._printer_state = None
        # Catch-all for low-frequency diagnostic fields (network blocks,
        # paired-printer identity, language) — kept loose so adding a new
        # field doesn't require a controller change.
        self._diagnostics = {}
        # Temperature-history ring (entries: (epoch_ts, chamber, target)).
        self._history = collections.deque(maxlen=HISTORY_MAX_SAMPLES)
        # Recent ``response`` frames — small ring so the UI can show the
        # last handful of command acknowledgements (set_hostname, etc.).
        self._responses = collections.deque(maxlen=20)

    # ---- listeners --------------------------------------------------

    def add_listener(self, callback):
        """Register ``callback(snapshot)`` for every state change."""
        self._listeners.append(callback)

    def set_control_sink(self, sink):
        """Install (or clear with ``None``) the alternate command transport.

        ``sink(verb, **params) -> bool``: return True if the send was
        handled, falsey to fall back to the WebSocket adapter. Used by the
        MQTT bridge so operational commands skip the WS reconnect cycle.
        Never receives safety frames (see ``__init__`` note).
        """
        self._control_sink = sink

    def _send(self, verb, **params):
        """Route an operational command through the sink, else the adapter.

        Falls back to the WebSocket adapter when no sink is installed, the
        sink declines (returns falsey), or the sink raises — so a broker
        hiccup degrades to the WS path rather than dropping the command.
        """
        sink = self._control_sink
        if sink is not None:
            try:
                if sink(verb, **params):
                    return
            except Exception:  # pylint: disable=broad-exception-caught
                self._log.warning(
                    "ChamberController: control sink failed for %s, "
                    "falling back to WebSocket",
                    verb,
                    exc_info=True,
                )
        self._adapter.send_command(verb, **params)

    def _notify(self):
        snapshot = self.snapshot()
        for cb in list(self._listeners):
            try:
                cb(snapshot)
            except Exception:  # pylint: disable=broad-exception-caught
                # Listener callbacks are arbitrary user code (plugin
                # message dispatch, UI updates) — one misbehaving
                # listener must not block the others or stop the
                # controller from accepting further updates.
                self._log.exception("ChamberController listener failed")

    # ---- public API -------------------------------------------------

    def snapshot(self):
        """Return a thread-safe dict of current controller state."""
        with self._lock:
            dry_remaining = self._extrapolated_dry_remaining()
            return {
                "chamber_temp": self._chamber_temp,
                "target_temp": self._target,
                "heater_on": self._heater_on,
                "mode": self._mode,
                "locked": self._locked,
                "connected": self._adapter.is_connected(),
                "last_safety_reason": self._last_safety_reason,
                "max_temp": self._max_temp,
                "observe_only": self._is_observe_only(),
                "fw_version": self._fw_version,
                "dry_target": self._dry_target,
                "dry_timer_hours": self._dry_timer_hours,
                "dry_remaining_s": dry_remaining,
                "bed_temp_limit": self._bed_temp_limit,
                "filter_threshold": self._filter_threshold,
                "printer_type": self._printer_type,
                "printer_state": self._printer_state,
                "diagnostics": dict(self._diagnostics),
                "responses": list(self._responses),
                "is_running": self._is_running,
            }

    def _extrapolated_dry_remaining(self):
        """Return ``_dry_remaining_s`` adjusted for wall-clock drift.

        Must be called while ``self._lock`` is held. While a dry cycle
        runs the device only ships ``remaining_seconds`` in big snapshot
        frames (post-reconnect), so the cached value goes stale by up to
        many minutes. We extrapolate from the last (ts, value) anchor
        captured during ``on_status`` — bounded below at 0 so the UI
        never sees negative values.
        """
        base = self._dry_remaining_s
        anchor = self._dry_remaining_anchor
        if base is None or anchor is None or not self._is_running:
            return base
        anchor_ts, anchor_remaining = anchor
        elapsed = int(time.monotonic() - anchor_ts)
        if elapsed <= 0:
            return base
        return max(anchor_remaining - elapsed, 0)

    def history_samples(self):
        """Return a list of recent (ts, chamber, target) samples."""
        with self._lock:
            return list(self._history)

    def _is_observe_only(self):
        return bool(getattr(self._adapter, "is_observe_only", lambda: False)())

    def set_target(self, value):
        """Push a new target temperature to the device.

        The Panda firmware has its own regulator (with the configured
        ``hotbedtemp`` / ``filtertemp`` thresholds in auto-mode); the
        plugin's job is to forward the user's intent, not to second-
        guess the on-device controller.
        """
        value = float(value)
        if value < DEVICE_TARGET_MIN:
            raise ValueError(f"target must be >= {DEVICE_TARGET_MIN:.0f}")
        # Upper bound is the lower of the device limit and the operator's
        # configurable safety cap.
        upper = min(DEVICE_TARGET_MAX, self._max_temp)
        if value > upper:
            raise ValueError(f"target exceeds max {upper:.1f}")
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("set_target", value=value)
        with self._lock:
            self._target = value
        self._notify()

    def set_mode(self, mode):
        """Switch the work-mode on the device (auto/manual/dry)."""
        if mode not in VALID_MODES:
            raise ValueError("invalid mode")
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        with self._lock:
            self._mode = mode
        self._send("set_mode", mode=mode)
        self._notify()

    def set_heater(self, on):
        """Turn the heater on or off, honouring lock and observe-only."""
        if self._locked and on:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._command_heater(bool(on))
        self._notify()

    def lock(self, reason="user"):
        """Engage the safety lock; shuts heater off unless observe-only."""
        with self._lock:
            self._locked = True
            self._last_safety_reason = reason
        # Outside observe-only mode, locking forces the heater off. In
        # observe-only mode the safety lock still flips (so the UI shows
        # the warning) but we do not push a heater_off frame to the
        # device — the operator may be debugging exactly that scenario.
        if not self._is_observe_only():
            self._command_heater(False, safety=True)
        self._notify()

    def emergency_stop(self, reason="estop"):
        """Hard stop: shut down the heater regardless of observe-only.

        Matches the Panda WebUI's behaviour for its own "Work Mode off"
        toggle, which is the only stop mechanism the device firmware
        exposes — a single ``work_on:false`` frame. The firmware
        internally stops any running dry cycle and turns the heater off
        on its own; sending additional frames (set_temp=0, isrunning=0)
        is redundant and not what the operator clicked on the device's
        own UI.
        """
        with self._lock:
            self._locked = True
            self._last_safety_reason = reason
            self._heater_on = False
            self._is_running = False
        # Bypass the observe-only suppression: heater_off is on the
        # observe-safe whitelist so the safety frame always reaches the
        # device, even when normal write traffic is suppressed.
        try:
            self._adapter.send_command("heater_off")
        except Exception:  # pylint: disable=broad-exception-caught
            # Emergency stop must not surface adapter/transport errors —
            # the internal state is already flipped to locked above, and
            # the user-facing safety guarantee holds regardless of
            # whether the wire frames made it out.
            self._log.exception("ChamberController: emergency_stop frame send failed")
        self._notify()

    def unlock(self):
        """Release the safety lock; does not change heater state."""
        with self._lock:
            self._locked = False
            self._last_safety_reason = None
        self._notify()

    def set_custom_dry(self, value, hours):
        """Set both custom dry target and timer atomically + commit.

        Sends ``filament_temp`` → ``filament_timer`` → ``commit_dry``
        in one transaction. Matches what the WebUI does when the user
        edits both fields and the captured commit-after-each-write
        contract, but combined into a single user-facing apply so the
        operator doesn't pay two reconnect cycles for one change.
        """
        value = float(value)
        hours = int(float(hours))
        if not DEVICE_DRY_TARGET_MIN <= value <= DEVICE_DRY_TARGET_MAX:
            raise ValueError(
                f"dry target must be {DEVICE_DRY_TARGET_MIN:.0f}-"
                f"{DEVICE_DRY_TARGET_MAX:.0f}"
            )
        if not DEVICE_DRY_TIMER_MIN <= hours <= DEVICE_DRY_TIMER_MAX:
            raise ValueError(
                f"dry timer must be {DEVICE_DRY_TIMER_MIN}-" f"{DEVICE_DRY_TIMER_MAX} h"
            )
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("set_dry_target", value=value)
        self._send("set_dry_timer", hours=hours)
        self._send("commit_dry")

    def select_preset_pla(self):
        """Select the device's built-in PLA dry preset."""
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("preset_pla")

    def select_preset_petg(self):
        """Select the device's built-in PETG/ABS dry preset."""
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("preset_petg")

    def set_filter_threshold(self, value):
        """Set the filter-fan activation threshold (in °C).

        The Panda turns its filter fan on once the paired printer's
        hotbed temperature crosses this value. Only meaningful in
        Auto-mode where the device acts on hotbed readings.
        """
        value = float(value)
        if not DEVICE_FILTER_THRESHOLD_MIN <= value <= DEVICE_FILTER_THRESHOLD_MAX:
            raise ValueError(
                f"filter threshold must be {DEVICE_FILTER_THRESHOLD_MIN:.0f}-"
                f"{DEVICE_FILTER_THRESHOLD_MAX:.0f}"
            )
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("set_filter_threshold", value=value)

    def set_heater_threshold(self, value):
        """Set the chamber-heater activation threshold (in °C)."""
        value = float(value)
        if not DEVICE_HEATER_THRESHOLD_MIN <= value <= DEVICE_HEATER_THRESHOLD_MAX:
            raise ValueError(
                f"heater threshold must be {DEVICE_HEATER_THRESHOLD_MIN:.0f}-"
                f"{DEVICE_HEATER_THRESHOLD_MAX:.0f}"
            )
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("set_heater_threshold", value=value)

    def start_drying(self):
        """Trigger the dry-mode timer / heater on the device.

        Mirrors the WebUI "Start Drying" button — a bare ``isrunning=1``
        write, no commit frame.
        """
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("start_drying")

    def stop_drying(self):
        """Stop an in-progress dry cycle on the device."""
        if self._locked:
            raise PermissionError("system locked")
        if self._is_observe_only():
            raise PermissionError("observe-only mode")
        self._send("stop_drying")

    def scan_printers(self):
        """Ask the device to (re-)scan the LAN for printers.

        Read-only from the heater's perspective — whitelisted in the
        adapter's observe-safe command set, so this works even with
        write frames otherwise suppressed.
        """
        self._adapter.send_command("scan_printers")

    def refresh_settings(self):
        """Force a fresh snapshot from the device.

        Drops the WebSocket and lets the adapter reconnect — the device
        only emits a full settings payload right after connect, so a
        plain ``get_settings`` on an established session is ignored.
        Costs a few seconds of disconnect time but is the only reliable
        refresh mechanism.
        """
        self._adapter.force_reconnect()

    def is_locked(self):
        """Return whether the safety lock is currently engaged."""
        return self._locked

    # ---- status pipeline -------------------------------------------

    def on_status(self, payload):
        """Consume a decoded status frame and re-run the control loop."""
        chamber_temp = payload.get("chamber_temp")
        heater_on = payload.get("heater_on")
        target_temp = payload.get("target_temp")
        mode = payload.get("mode")
        # Auto-release watchdog-induced locks: a fresh status frame means
        # the data flow recovered, so the safety reason is no longer
        # valid. Manual locks (reason='user') and emergency stops
        # (reason='estop') stay engaged until the operator releases them.
        if self._locked and self._last_safety_reason == "timeout":
            self._log.info(
                "ChamberController: data flow recovered, releasing watchdog lock"
            )
            self.unlock()
        with self._lock:
            if chamber_temp is not None:
                try:
                    self._chamber_temp = float(chamber_temp)
                except (TypeError, ValueError):
                    pass
            if heater_on is not None:
                self._heater_on = bool(heater_on)
            # Sync mode + target from the device snapshot. Without this
            # the controller's internal state drifts from the device on
            # startup (default mode=auto) or when the user changes the
            # mode via the device's own WebUI, leaving the UI showing a
            # stale mode.
            if mode is not None and mode in VALID_MODES:
                self._mode = mode
            if target_temp is not None:
                try:
                    self._target = float(target_temp)
                except (TypeError, ValueError):
                    pass
            # Pass-through firmware extras — protocol.py has already
            # cast them; store as-is and let the UI decide.
            for key in (
                "fw_version",
                "dry_target",
                "dry_timer_hours",
                "dry_remaining_s",
                "bed_temp_limit",
                "filter_threshold",
                "is_running",
                "printer_type",
                "printer_state",
            ):
                if key in payload:
                    setattr(self, "_" + key, payload[key])
            # Refresh the dry-remaining anchor whenever a snapshot brings
            # us a fresh remaining_seconds. Drop it when the cycle is
            # not running so we don't extrapolate against a paused or
            # post-reset value.
            if "dry_remaining_s" in payload and self._is_running:
                self._dry_remaining_anchor = (
                    time.monotonic(),
                    int(payload["dry_remaining_s"]),
                )
            elif not self._is_running:
                self._dry_remaining_anchor = None
            # Network / pairing / language diagnostics — merged into the
            # rolling diagnostics dict so the latest known value sticks
            # even when subsequent status frames are slimmer.
            for key in (
                "language",
                "net_sta_ip",
                "net_sta_hostname",
                "net_sta_state",
                "net_ap_ssid",
                "net_ap_ip",
                "net_ap_on",
                "net_wifi_ssid",
                "printer_name",
                "printer_host",
                "printer_port",
                "printer_scan",
                "printer_list",
                # HA/MQTT broker mirror (V1.0.4+); password never included.
                "ha_ip",
                "ha_port",
                "ha_user",
                "ha_state",
            ):
                if key in payload:
                    self._diagnostics[key] = payload[key]
            if "response" in payload and isinstance(payload["response"], dict):
                self._responses.append(payload["response"])
            # Append a history sample whenever we have a chamber reading.
            if self._chamber_temp is not None:
                self._history.append((time.time(), self._chamber_temp, self._target))
        self._check_safety_limits()
        self._notify()

    def watchdog_tick(self):
        """Called periodically by the plugin. Detects stale data."""
        last_rx = self._adapter.last_rx_timestamp()
        if last_rx == 0:
            return
        if time.time() - last_rx > self._timeout:
            if not self._locked:
                self._log.warning(
                    "ChamberController: no data for %.1fs — "
                    "locking and shutting heater off",
                    self._timeout,
                )
                self.lock(reason="timeout")

    # ---- internals --------------------------------------------------

    def _check_safety_limits(self):
        with self._lock:
            temp = self._chamber_temp
        if temp is not None and temp > self._max_temp:
            self._log.error(
                "ChamberController: chamber %.1f exceeds hard limit %.1f — locking",
                temp,
                self._max_temp,
            )
            self.lock(reason="over_temp")

    def _command_heater(self, on, safety=False):
        """Send a heater on/off frame.

        ``safety=True`` (lock-driven heater-off) always goes straight to
        the WebSocket adapter — it must not depend on the MQTT broker.
        Normal user toggles (``safety=False``) use the control sink when
        installed, falling back to the WebSocket.
        """
        with self._lock:
            self._heater_on = bool(on)
        cmd = "heater_on" if on else "heater_off"
        if safety:
            self._adapter.send_command(cmd)
        else:
            self._send(cmd)
