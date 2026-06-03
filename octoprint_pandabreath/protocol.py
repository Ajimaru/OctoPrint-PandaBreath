# coding=utf-8
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
PandaProtocolAdapter — WebSocket transport for the BIQU Panda Breath heater.

The on-the-wire framing implemented here is derived from two upstream
projects (see the file header above for full attribution). Concretely:

* From https://github.com/jeng37/BIQU-Panda-Breath-Mod (Panda.py):
  - Bind frame layout ``{"printer": {"ip", "sn", "access_code"}}``
  - Settings frame ``{"settings": {"work_on", "work_mode", "set_temp"}}``
  - Status field names (``warehouse_temper``, ``set_temp``, ``work_mode``,
    ``work_on``, ``fw_version``)
  - Work-mode mapping: 1=Auto, 2=Manual, 3=Dry, 0=Standby

* From https://github.com/bula87/chamber_control (chamber_control.py):
  - ``{"query": 1}`` poll frame for read-only status pulls
  - Reconnect/poll cadence

Transport modes:

* ``client`` — plugin actively connects to ``ws://<panda_ip>/ws``. This is
  the mode that matches real hardware.
* ``server`` — plugin listens for inbound connections (intended for setups
  where the heater is configured to push to the plugin host, mirroring the
  Bambu-emulation approach in BIQU-Panda-Breath-Mod). For real Panda
  firmware "client" is what you want.
"""
from __future__ import absolute_import

# pylint: disable=broad-exception-caught,invalid-name
# pylint: disable=missing-function-docstring

import collections
import json
import logging
import ssl
import threading
import time
from typing import Any, Dict, cast

try:
    import websocket  # type: ignore  # from websocket-client
except Exception:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

try:
    import asyncio  # type: ignore
    import websockets  # type: ignore
except Exception:  # pragma: no cover
    websockets = None  # type: ignore[assignment]
    asyncio = None  # type: ignore[assignment]


MODE_SERVER = "server"
MODE_CLIENT = "client"

# Panda status payload is tiny; cap to defend against probes.
MAX_FRAME_BYTES = 4 * 1024

# Work-mode integer codes used by Panda firmware.
WORK_MODE_STANDBY = 0
WORK_MODE_AUTO = 1
WORK_MODE_MANUAL = 2
WORK_MODE_DRY = 3

_MODE_NAME_TO_CODE = {
    "auto": WORK_MODE_AUTO,
    "manual": WORK_MODE_MANUAL,
    "dry": WORK_MODE_DRY,
    "standby": WORK_MODE_STANDBY,
}

_CODE_TO_MODE_NAME = {v: k for k, v in _MODE_NAME_TO_CODE.items()}


class PandaProtocolAdapter(object):
    """Transport + framing for the Panda Breath device.

    The adapter owns a background thread. It surfaces inbound device state
    via ``on_status`` and accepts outbound commands via
    :meth:`send_command`. Reconnect handling lives inside the adapter so
    callers do not have to deal with it.
    """

    # Commands that are always allowed, even in observe-only mode. ``bind``
    # is needed to satisfy the device's pairing handshake (otherwise the
    # Panda ignores us entirely), ``query`` is a read-only status pull and
    # ``heater_off`` / ``set_target`` (target=0) are escape hatches used by
    # the navbar emergency stop — a safety button is worthless if it can be
    # silenced by a configuration toggle.
    _OBSERVE_SAFE_COMMANDS = frozenset(
        {"bind", "query", "get_settings", "heater_off",
         "set_target", "scan_printers"}
    )

    def __init__(
        self,
        mode=MODE_CLIENT,
        host="127.0.0.1",
        port=8765,
        client_url=None,
        serial_number=None,
        access_code=None,
        host_ip=None,
        on_status=None,
        on_connection_change=None,
        on_frame=None,
        on_frame_persist=None,
        logger=None,
        reconnect_delay=5.0,
        observe_only=False,
        debug_enabled_getter=None,
        tls_enabled=False,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_insecure=False,
    ):
        self._mode = mode
        self._host = host
        self._port = int(port)
        self._client_url = client_url
        self._serial_number = serial_number
        self._access_code = access_code
        self._host_ip = host_ip
        self._on_status = on_status or (lambda payload: None)
        self._on_connection_change = on_connection_change or (lambda c: None)
        self._on_frame = on_frame or (lambda direction, frame: None)
        # ``on_frame_persist`` receives the full, untruncated frame string
        # for disk logging. ``on_frame`` continues to deliver the truncated
        # variant to the UI ring buffer.
        self._on_frame_persist = (
            on_frame_persist or (lambda direction, frame: None)
        )
        self._log = logger or logging.getLogger(__name__)
        self._reconnect_delay = float(reconnect_delay)
        self._observe_only = bool(observe_only)
        # Returns the current ``debug_panel_enabled`` setting. Used only to
        # pick the log level for the *expected* reconnect-reset that
        # ``force_reconnect`` deliberately causes: WARNING when the operator
        # has the debug panel open, INFO otherwise. Read live (not cached)
        # so toggling the setting takes effect without a stack restart.
        self._debug_enabled_getter = debug_enabled_getter or (lambda: False)
        self._tls_enabled = bool(tls_enabled)
        self._tls_ca_file = tls_ca_file or None
        self._tls_cert_file = tls_cert_file or None
        self._tls_key_file = tls_key_file or None
        self._tls_insecure = bool(tls_insecure)

        self._thread = None
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()
        self._active_socket = None
        self._active_loop = None
        self._connected = False
        self._last_rx = 0.0
        # Coalesce reconnect-loop log spam: keep logging at WARNING for
        # the first occurrence and then downgrade subsequent identical
        # errors to DEBUG until the situation changes (different error
        # or successful connect). Counts how many were suppressed so we
        # can summarise on the next state change.
        self._last_error_signature = None
        self._suppressed_error_count = 0
        # Set by ``force_reconnect`` right before it closes the socket on
        # purpose, so the resulting "connection reset by peer" is logged as
        # an expected reconnect rather than an unexpected failure. Cleared
        # once that expected error has been consumed by the run loop.
        self._expecting_close = False
        # Bounded ring of recent frames for the debug panel. Entries:
        # (timestamp_float, direction "rx"/"tx", raw_str).
        self._frame_history = collections.deque(maxlen=50)
        self._history_lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        target = (
            self._run_server if self._mode == MODE_SERVER else self._run_client
        )
        self._thread = threading.Thread(
            target=target, name="PandaProtocolAdapter", daemon=True
        )
        self._thread.start()

    def force_reconnect(self):
        """Drop the active socket so the run-loop reconnects immediately.

        The Panda firmware only emits a full ``get_settings`` snapshot
        right after a fresh WebSocket connect — re-sending the request
        on an established session is silently ignored. Closing the
        socket from here makes the reconnect loop wake up, redo the
        bind handshake and pull a fresh snapshot, which is the only
        reliable way to refresh runtime state on demand.
        """
        # Reset the error-coalesce state so the (expected) "connection
        # closed by peer" we are about to cause doesn't get folded into
        # a previous unreachable streak.
        self._last_error_signature = None
        self._suppressed_error_count = 0
        # Mark the close we are about to cause as expected, so the run
        # loop's error handler downgrades it from WARNING to INFO/DEBUG.
        self._expecting_close = True
        self._close_active_socket()

    def stop(self):
        self._stop_event.set()
        self._close_active_socket()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._set_connected(False)

    def is_connected(self):
        return self._connected

    def last_rx_timestamp(self):
        """Time of the last decoded RX frame, or 0 if not connected.

        Returning 0 while disconnected (or just after a forced
        reconnect) makes the controller-side watchdog skip its
        staleness check — the connection itself is the right signal
        for "data is not flowing", not a timer rooted in a value from
        before the socket dropped.
        """
        if not self._connected:
            return 0.0
        return self._last_rx

    def is_observe_only(self):
        return self._observe_only

    def get_frame_history(self):
        """Return a snapshot of recent frames for the debug panel."""
        with self._history_lock:
            return [
                {"ts": ts, "dir": direction, "frame": frame}
                for ts, direction, frame in self._frame_history
            ]

    # JSON keys whose values must never leave the adapter: WiFi creds
    # echoed by the Panda's get_settings response, and the pairing
    # access_code from our own bind frame. Redacted before any logging,
    # UI broadcast or disk persistence happens.
    _REDACT_KEYS = ("password", "access_code")

    @classmethod
    def _redact_frame(cls, frame):
        """Replace any password / access_code values in a JSON frame.

        Operates on the string. If the frame is not parseable JSON we
        return it unchanged — better to keep raw text than to invent a
        partial redaction that misses some occurrence.
        """
        if not frame or not any(k in frame for k in cls._REDACT_KEYS):
            return frame
        try:
            decoded = json.loads(frame)
        except (TypeError, ValueError):
            return frame

        def _walk(node):
            if isinstance(node, dict):
                for k in list(node.keys()):
                    if k in cls._REDACT_KEYS:
                        if node[k]:
                            node[k] = "<redacted>"
                    else:
                        _walk(node[k])
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(decoded)
        return json.dumps(decoded, separators=(",", ":"))

    def _record_frame(self, direction, frame):
        if isinstance(frame, (bytes, bytearray)):
            try:
                frame = frame.decode("utf-8", errors="replace")
            except Exception:
                frame = "<binary>"
        if frame is None:
            return
        # Strip credentials before anything else touches the frame.
        frame = self._redact_frame(frame)
        # Persist the full frame before truncation — the disk log is for
        # long captures and reverse engineering, where '…' suffixes would
        # destroy the JSON.
        try:
            self._on_frame_persist(direction, frame)
        except Exception:
            self._log.exception(
                "PandaProtocolAdapter: on_frame_persist callback failed"
            )
        # Truncate the in-memory copy so the debug panel cannot be DoS'd
        # by a chatty peer.
        if len(frame) > 512:
            frame = frame[:512] + "…"
        entry = (time.time(), direction, frame)
        with self._history_lock:
            self._frame_history.append(entry)
        try:
            self._on_frame(direction, frame)
        except Exception:
            self._log.exception(
                "PandaProtocolAdapter: on_frame callback failed"
            )

    def _build_ssl_context(self, server_side):
        """Construct an SSL context for the configured TLS settings."""
        if not self._tls_enabled:
            return None
        if server_side:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            if not (self._tls_cert_file and self._tls_key_file):
                raise RuntimeError(
                    "TLS server mode requires tls_cert_file + tls_key_file"
                )
            ctx.load_cert_chain(self._tls_cert_file, self._tls_key_file)
            if self._tls_ca_file:
                ctx.load_verify_locations(self._tls_ca_file)
        else:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            if self._tls_ca_file:
                ctx.load_verify_locations(self._tls_ca_file)
            if self._tls_cert_file and self._tls_key_file:
                ctx.load_cert_chain(self._tls_cert_file, self._tls_key_file)
            if self._tls_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ---- outbound (high-level commands) -----------------------------

    def send_command(self, command, **params):
        """Translate a high-level command into a Panda settings frame.

        Returns True if the frame was handed to the socket without raising.
        Write commands are suppressed in observe-only mode; only ``bind``
        and ``query`` remain allowed so the device pairing handshake and
        status polls still work.
        """
        if self._observe_only and command not in self._OBSERVE_SAFE_COMMANDS:
            self._log.info(
                "PandaProtocolAdapter: observe-only — suppressing %s",
                command,
            )
            return False
        frame = self._build_frame(command, params)
        if frame is None:
            self._log.debug(
                "PandaProtocolAdapter: dropping unknown command %s", command
            )
            return False
        return self._send_raw(frame)

    def _build_frame(self, command, params):
        """Map a controller command to the JSON wire format used by Panda."""
        if command == "heater_on":
            # work_on as JSON boolean — see chamber_control.py line 195.
            return {"settings": {"work_on": True}}
        if command == "heater_off":
            return {"settings": {"work_on": False}}
        if command == "set_target":
            value = int(float(params.get("value", 0)))
            return {"settings": {"set_temp": value}}
        if command == "set_mode":
            mode_name = params.get("mode")
            code = _MODE_NAME_TO_CODE.get(mode_name)
            if code is None:
                return None
            return {"settings": {"work_mode": code}}
        if command == "bind":
            payload = {}
            if self._host_ip:
                payload["ip"] = self._host_ip
            if self._serial_number:
                payload["sn"] = self._serial_number
            if self._access_code:
                payload["access_code"] = self._access_code
            return {"printer": payload}
        if command == "query":
            return {"query": 1}
        if command == "get_settings":
            # Full settings pull — observed in BIQU-Panda-Breath-Mod's
            # initial handshake. Returns a larger settings payload than
            # ``query:1``, including dry-mode and filter fields when the
            # firmware supports them.
            return {"get_settings": 1}
        if command == "set_dry_target":
            # Captured from the device's own WebUI: it writes the
            # ``filament_temp`` key, not ``custom_temp``. The read-side
            # field name (``custom_temp``) is asymmetric — the firmware
            # surfaces it under a different key than it accepts.
            value = int(float(params.get("value", 0)))
            return {"settings": {"filament_temp": value}}
        if command == "set_dry_timer":
            # Same asymmetry as set_dry_target: write key is
            # ``filament_timer``, read echoes under ``custom_timer``.
            value = int(float(params.get("hours", 0)))
            return {"settings": {"filament_timer": value}}
        if command == "set_filter_threshold":
            # WebUI "Filter Fan Activation Threshold Temperature" — the
            # *printer's* hotbed temperature above which the Panda fan
            # turns on. Single write, no commit needed.
            value = int(float(params.get("value", 0)))
            return {"settings": {"filtertemp": value}}
        if command == "set_heater_threshold":
            # WebUI "Heater Activation Threshold Temperature" — printer
            # hotbed temp above which the Panda chamber heater engages.
            # The field name ``hotbedtemp`` is misleading: it isn't the
            # current bed temperature, it's the trigger threshold.
            value = int(float(params.get("value", 0)))
            return {"settings": {"hotbedtemp": value}}
        if command == "preset_pla":
            # Captured from WebUI's PLA button. Loads device-internal
            # PLA-defaults — no temp/timer needed in the frame.
            return {"settings": {"filament_drying_mode": 1}}
        if command == "preset_petg":
            # WebUI's PETG/ABS button — device-internal defaults.
            return {"settings": {"filament_drying_mode": 2}}
        if command == "commit_dry":
            # Captured commit frame the WebUI emits after every custom
            # dry-mode write — also marks "custom mode" selected. The
            # device silently discards filament_temp/filament_timer
            # writes if this commit doesn't follow.
            return {"settings": {"filament_drying_mode": 3}}
        if command == "start_drying":
            # Captured WebUI behaviour: pressing "Start Drying" sends a
            # bare isrunning=1 frame; no commit needed.
            return {"settings": {"isrunning": 1}}
        if command == "stop_drying":
            return {"settings": {"isrunning": 0}}
        if command == "scan_printers":
            # Tells the device to (re-)scan the LAN for printers. The next
            # status frame echoes ``printer.scan: 1`` and eventually the
            # refreshed ``printer.list``.
            return {"printer": {"scan": 1}}
        return None

    def _send_raw(self, payload):
        frame = json.dumps(payload)
        with self._send_lock:
            sock = self._active_socket
            loop = self._active_loop
        if sock is None:
            # Expected during a reconnect window — the controller and
            # watchdog keep trying to push state every couple of seconds.
            # DEBUG only; the WARNING-level reason is already covered by
            # the dedup'd reconnect-error logging.
            self._log.debug(
                "PandaProtocolAdapter: drop frame, no active socket"
            )
            return False
        try:
            if loop is not None and asyncio is not None:
                # In server mode ``sock`` is a websockets.WebSocketServer-
                # Protocol whose ``.send`` returns a coroutine; in client
                # mode it is a synchronous websocket-client socket. The
                # branch is selected by whether a loop is active, but the
                # type checker cannot see that — cast to silence it.
                asyncio.run_coroutine_threadsafe(
                    cast(Any, sock.send(frame)), loop
                ).result(timeout=2.0)
            else:
                sock.send(frame)
            self._record_frame("tx", frame)
            return True
        except Exception as exc:
            self._log.warning("PandaProtocolAdapter: send failed: %s", exc)
            return False

    # ---- inbound ----------------------------------------------------

    def _decode_frame(self, raw):
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_FRAME_BYTES:
                self._log.warning(
                    "PandaProtocolAdapter: oversized binary frame dropped"
                )
                return None
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str) and len(raw) > MAX_FRAME_BYTES:
            self._log.warning(
                "PandaProtocolAdapter: oversized text frame dropped"
            )
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            self._log.debug("PandaProtocolAdapter: non-JSON frame ignored")
            return None

    def _normalise_status(self, decoded):
        """Project a Panda settings frame into a stable controller payload.

        Field names below come from BIQU-Panda-Breath-Mod (Panda.py) and
        chamber_control (chamber_control.py).

        Two inbound shapes are handled:

        * ``{"settings": {...}}`` — the bulk of the device traffic
          (chamber temperature, mode, dry-mode echoes etc.).
        * ``{"printer": {"state": N}}`` — pairing/connection status of
          the paired printer the Panda is bound to. Observed values from
          real hardware:

          - ``2`` is emitted when the device successfully reaches the
            paired printer (or right after the user presses *Printer ON*
            on the Panda's hardware buttons).
          - ``4`` is emitted when the paired printer is offline /
            unreachable (or right after *Printer OFF*).

          The exact code table is not publicly documented; we surface
          the raw integer and let the UI render a best-guess label.
        """
        out: Dict[str, Any] = {"type": "status"}
        # Command-acknowledgement frames: emitted by the device after
        # config changes performed through the Panda's own web UI (e.g.
        # set_hostname). Shape observed on real hardware:
        #   {"response": {"type": "set_hostname", "ok": 1}}
        # We surface them so the diagnostics tab can list recent confirms.
        response = decoded.get("response")
        if isinstance(response, dict) and "type" in response:
            ok_val = response.get("ok")
            try:
                ok_val = int(ok_val) if ok_val is not None else None
            except (TypeError, ValueError):
                ok_val = None
            out["response"] = {
                "type": str(response.get("type") or ""),
                "ok": ok_val,
                "ts": time.time(),
            }
        printer = decoded.get("printer")
        if isinstance(printer, dict):
            if "state" in printer:
                try:
                    out["printer_state"] = int(printer["state"])
                except (TypeError, ValueError):
                    pass
            # The big get_settings response also carries the paired
            # printer's identity (Klipper/Moonraker host + port + name)
            # and the scan list of nearby printers.
            for key in ("name", "host", "port", "scan"):
                if key in printer:
                    out["printer_" + key] = printer[key]
            if isinstance(printer.get("list"), list):
                out["printer_list"] = [
                    {
                        "name": p.get("name"),
                        "ip": p.get("ip"),
                        "port": p.get("port"),
                    }
                    for p in printer["list"]
                    if isinstance(p, dict)
                ]
        # Network blocks — only present in the initial get_settings reply.
        sta = decoded.get("sta")
        if isinstance(sta, dict):
            if "ip" in sta:
                out["net_sta_ip"] = sta["ip"]
            if "hostname" in sta:
                out["net_sta_hostname"] = sta["hostname"]
            if "state" in sta:
                try:
                    out["net_sta_state"] = int(sta["state"])
                except (TypeError, ValueError):
                    pass
        ap = decoded.get("ap")
        if isinstance(ap, dict):
            if "ssid" in ap:
                out["net_ap_ssid"] = ap["ssid"]
            if "ip" in ap:
                out["net_ap_ip"] = ap["ip"]
            if "on" in ap:
                try:
                    out["net_ap_on"] = bool(int(ap["on"]))
                except (TypeError, ValueError):
                    pass
        wifi = decoded.get("wifi")
        if isinstance(wifi, dict) and "ssid" in wifi:
            out["net_wifi_ssid"] = wifi["ssid"]
        # HA / MQTT broker block (firmware V1.0.4+). Read-only mirror of the
        # broker the device is bound to; used by the plugin to pre-fill its
        # own MQTT settings. The password is never surfaced (it is redacted
        # upstream and must not enter the snapshot).
        ha = decoded.get("ha")
        if isinstance(ha, dict):
            if "ip" in ha:
                out["ha_ip"] = ha["ip"]
            if "port" in ha:
                try:
                    out["ha_port"] = int(ha["port"])
                except (TypeError, ValueError):
                    pass
            if "user" in ha:
                out["ha_user"] = ha["user"]
            if "state" in ha:
                try:
                    out["ha_state"] = int(ha["state"])
                except (TypeError, ValueError):
                    pass
        settings = decoded.get("settings")
        if not isinstance(settings, dict):
            return out if len(out) > 1 else None
        if "language" in settings:
            out["language"] = settings["language"]
        if "warehouse_temper" in settings:
            try:
                out["chamber_temp"] = float(settings["warehouse_temper"])
            except (TypeError, ValueError):
                pass
        if "set_temp" in settings:
            try:
                out["target_temp"] = float(settings["set_temp"])
            except (TypeError, ValueError):
                pass
        if "work_on" in settings:
            wo = settings["work_on"]
            out["heater_on"] = bool(wo) and wo not in (0, "0")
        if "work_mode" in settings:
            try:
                out["mode"] = _CODE_TO_MODE_NAME.get(
                    int(settings["work_mode"]), "standby"
                )
            except (TypeError, ValueError):
                pass
        if "fw_version" in settings:
            out["fw_version"] = settings["fw_version"]
        # Optional / firmware-dependent fields. We just pass them through
        # as floats/ints/strings; the UI decides whether to surface them.
        #
        # Names below are confirmed against a real Panda Breath capture:
        # ``custom_temp`` / ``custom_timer`` are the dry-mode pair the
        # device actually reports — the ``filament_*`` keys from the
        # BIQU-Panda-Breath-Mod doc are kept as a legacy fallback in case
        # alternate firmwares use them.
        for key, out_key, caster in (
            ("custom_temp", "dry_target", float),
            ("custom_timer", "dry_timer_hours", int),
            ("filament_temp", "dry_target", float),
            ("filament_timer", "dry_timer_hours", int),
            ("remaining_seconds", "dry_remaining_s", int),
            ("hotbedtemp", "bed_temp_limit", float),
            ("filtertemp", "filter_threshold", float),
            ("isrunning", "is_running", bool),
            ("printer_type", "printer_type", int),
        ):
            if key in settings:
                try:
                    if caster is bool:
                        raw = settings[key]
                        out[out_key] = bool(raw) and raw not in (0, "0")
                    else:
                        out[out_key] = caster(settings[key])
                except (TypeError, ValueError):
                    pass
        return out if len(out) > 1 else None

    def _handle_inbound(self, raw, peer_authenticated):
        self._record_frame("rx", raw)
        decoded = self._decode_frame(raw)
        if not decoded:
            return peer_authenticated
        self._last_rx = time.time()

        # Server-mode pairing gate: an unauthenticated peer can only send a
        # bind frame carrying the access_code. Until that arrives any
        # ``settings`` payloads are rejected so an attacker cannot spoof
        # chamber_temp readings.
        if self._access_code and not peer_authenticated:
            printer = decoded.get("printer") or {}
            if printer.get("access_code") == self._access_code:
                peer_authenticated = True
                self._log.info(
                    "PandaProtocolAdapter: peer authenticated (sn=%s)",
                    printer.get("sn"),
                )
            else:
                self._log.warning(
                    "PandaProtocolAdapter: rejecting frame before bind"
                )
            return peer_authenticated

        snapshot = self._normalise_status(decoded)
        if snapshot is not None:
            try:
                self._on_status(snapshot)
            except Exception:
                self._log.exception(
                    "PandaProtocolAdapter: on_status callback failed"
                )
        return peer_authenticated

    # ---- server mode (Bambu-emulation style listener) --------------

    def _run_server(self):
        if websockets is None or asyncio is None:
            self._log.error(
                "PandaProtocolAdapter: server mode needs the "
                "'websockets' package"
            )
            return
        while not self._stop_event.is_set():
            try:
                asyncio.run(self._serve_forever())
            except Exception as exc:
                self._log_reconnect_error(exc)
            if self._stop_event.is_set():
                break
            self._backoff_sleep()

    async def _serve_forever(self):
        aio = asyncio
        wss = websockets
        assert aio is not None and wss is not None
        self._active_loop = aio.get_running_loop()
        stop_future = self._active_loop.create_future()

        async def _on_client(socket):
            peer_authenticated = not bool(self._access_code)
            with self._send_lock:
                self._active_socket = socket
            self._set_connected(True)
            try:
                async for message in socket:
                    peer_authenticated = self._handle_inbound(
                        message, peer_authenticated
                    )
            finally:
                with self._send_lock:
                    if self._active_socket is socket:
                        self._active_socket = None
                self._set_connected(False)

        async def _stop_watcher():
            while not self._stop_event.is_set():
                await aio.sleep(0.5)
            if not stop_future.done():
                stop_future.set_result(True)

        try:
            ssl_ctx = self._build_ssl_context(server_side=True)
        except Exception as exc:
            self._log.error(
                "PandaProtocolAdapter: TLS setup failed: %s", exc
            )
            return
        scheme = "wss" if ssl_ctx is not None else "ws"
        async with wss.serve(
            _on_client, self._host, self._port, ssl=ssl_ctx
        ):
            self._log.info(
                "PandaProtocolAdapter: listening on %s://%s:%s",
                scheme,
                self._host,
                self._port,
            )
            watcher = aio.create_task(_stop_watcher())
            try:
                await stop_future
            finally:
                watcher.cancel()
        self._active_loop = None

    # ---- client mode (talks to ws://<panda_ip>/ws) ------------------

    def _run_client(self):
        wsmod = websocket
        if wsmod is None:
            self._log.error(
                "PandaProtocolAdapter: client mode needs the "
                "'websocket-client' package"
            )
            return
        while not self._stop_event.is_set():
            url = self._client_url
            if not url:
                self._log.error(
                    "PandaProtocolAdapter: client mode requires client_url"
                )
                return
            try:
                # Only announce the connect on the first attempt of a
                # new error streak — otherwise this line spams alongside
                # the warnings during long outages.
                if self._last_error_signature is None:
                    self._log.info(
                        "PandaProtocolAdapter: connecting to %s", url
                    )
                else:
                    self._log.debug(
                        "PandaProtocolAdapter: reconnect attempt to %s", url
                    )
                connect_kwargs: Dict[str, Any] = {"timeout": 5}
                if url.startswith("wss://"):
                    sslopt: Dict[str, Any] = {}
                    if self._tls_ca_file:
                        sslopt["ca_certs"] = self._tls_ca_file
                    if self._tls_cert_file and self._tls_key_file:
                        sslopt["certfile"] = self._tls_cert_file
                        sslopt["keyfile"] = self._tls_key_file
                    if self._tls_insecure:
                        sslopt["cert_reqs"] = ssl.CERT_NONE
                        sslopt["check_hostname"] = False
                    if sslopt:
                        connect_kwargs["sslopt"] = sslopt
                ws = wsmod.create_connection(url, **connect_kwargs)
                with self._send_lock:
                    self._active_socket = ws
                self._set_connected(True)
                self._reset_error_log_state("connected")

                # Bind handshake — see Panda.py:644-665. Without this the
                # heater ignores subsequent settings frames.
                bind_frame = self._build_frame("bind", {}) or {}
                if bind_frame.get("printer"):
                    payload = json.dumps(bind_frame)
                    ws.send(payload)
                    self._record_frame("tx", payload)
                # Pull full settings once so the controller has a complete
                # snapshot — get_settings returns more fields than the
                # lightweight ``query:1`` poll.
                getset_payload = json.dumps(
                    self._build_frame("get_settings", {})
                )
                ws.send(getset_payload)
                self._record_frame("tx", getset_payload)

                # In client mode we initiated the connection, so the remote
                # endpoint is operator-controlled and considered trusted.
                peer_authenticated = True

                # Periodic keepalive: real-hardware captures show the
                # Panda occasionally stops broadcasting for >15 s without
                # closing the TCP socket. A bare ``query`` frame nudges
                # it to respond, keeping ``_last_rx`` fresh so the
                # controller's staleness watchdog doesn't false-positive.
                last_keepalive = time.monotonic()
                keepalive_interval = 5.0

                while not self._stop_event.is_set():
                    ws.settimeout(1.0)
                    try:
                        raw = ws.recv()
                    except wsmod.WebSocketTimeoutException:
                        # No data this second — check if it's time for a
                        # keepalive ping.
                        now = time.monotonic()
                        if now - last_keepalive >= keepalive_interval:
                            last_keepalive = now
                            # Any send failure here propagates to the
                            # outer except, which triggers the reconnect
                            # path — exactly what we want when the
                            # socket is dead.
                            payload = json.dumps(
                                self._build_frame("query", {})
                            )
                            ws.send(payload)
                            self._record_frame("tx", payload)
                        continue
                    if not raw:
                        break
                    peer_authenticated = self._handle_inbound(
                        raw, peer_authenticated
                    )
            except Exception as exc:
                self._log_reconnect_error(exc)
            finally:
                self._close_active_socket()
                self._set_connected(False)
            if self._stop_event.is_set():
                break
            self._backoff_sleep()

    # ---- helpers ----------------------------------------------------

    # Cap on the exponential reconnect backoff. After this long between
    # attempts a transient flap reconverges fast enough on the next try.
    _RECONNECT_BACKOFF_CAP = 60.0

    def _backoff_sleep(self):
        """Sleep before the next reconnect, with exponential backoff.

        Resets to the base ``reconnect_delay`` whenever a connect
        succeeds (handled by :meth:`_reset_error_log_state`). During a
        long outage we cap at ``_RECONNECT_BACKOFF_CAP`` so even a
        permanently offline device costs us a couple of log lines per
        minute, not per ``reconnect_delay``.
        """
        # ``_suppressed_error_count`` counts repeats of the *same*
        # bucket; total attempts in this outage is roughly that + the
        # initial one. Doubling per attempt is a fine approximation.
        attempt = max(1, self._suppressed_error_count + 1)
        delay = min(
            self._RECONNECT_BACKOFF_CAP,
            self._reconnect_delay * (2 ** min(attempt - 1, 8)),
        )
        # Honour stop_event in small slices so shutdown stays responsive.
        end = time.monotonic() + delay
        while not self._stop_event.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))

    # Errno values that all mean "device not reachable on the network".
    # Collapsed into a single bucket so a flapping link doesn't rotate
    # log spam between Host-is-down / No-route / timed-out.
    _UNREACHABLE_ERRNOS = frozenset({
        # POSIX / BSD networking errors observed against an offline Panda
        50,   # ENETDOWN
        51,   # ENETUNREACH
        60,   # ETIMEDOUT
        61,   # ECONNREFUSED
        64,   # EHOSTDOWN
        65,   # EHOSTUNREACH
        110,  # ETIMEDOUT (Linux)
        111,  # ECONNREFUSED (Linux)
        112,  # EHOSTDOWN (Linux)
        113,  # EHOSTUNREACH (Linux)
    })

    @classmethod
    def _classify_error(cls, exc):
        """Bucket an exception into a stable category for log coalescing.

        Returns ``("unreachable", human_text)`` for any of the well-known
        network-reachability errnos, ``("timeout", text)`` for socket
        timeouts (different exception type than OSError on some Python
        builds), and ``(repr_of_type, text)`` as a fallback. The bucket
        is what dedup compares — the human text is what we log.
        """
        msg = str(exc) or exc.__class__.__name__
        errno_val = getattr(exc, "errno", None)
        if errno_val in cls._UNREACHABLE_ERRNOS:
            return "unreachable", msg
        # ``socket.timeout`` / ``TimeoutError`` carry no errno.
        lower = msg.lower()
        if "timed out" in lower or "timeout" in lower:
            return "unreachable", msg
        if "no route to host" in lower or "host is down" in lower:
            return "unreachable", msg
        return type(exc).__name__, msg

    def _log_reconnect_error(self, exc):
        """Log a reconnect-loop error, coalescing repeated buckets.

        First occurrence of a bucket is logged at WARNING with the full
        text. Subsequent failures in the same bucket are DEBUG-only and
        counted. When the bucket changes (or a successful connect calls
        :meth:`_reset_error_log_state`) we emit a summary of how many
        were hidden, then log the new bucket.

        An expected close (one we deliberately caused via
        :meth:`force_reconnect`) is not a failure: it is logged at WARNING
        when the debug panel is enabled and INFO otherwise, and never
        enters the dedup bucket state used for genuine connection errors.
        """
        bucket, text = self._classify_error(exc)
        if self._expecting_close:
            self._expecting_close = False
            level = (
                logging.WARNING
                if self._debug_enabled_getter()
                else logging.INFO
            )
            self._log.log(
                level,
                "PandaProtocolAdapter: expected reconnect (%s): %s",
                bucket,
                text,
            )
            return
        if bucket == self._last_error_signature:
            self._suppressed_error_count += 1
            self._log.debug(
                "PandaProtocolAdapter: client error (suppressed): %s",
                text,
            )
            return
        if self._suppressed_error_count > 0:
            self._log.warning(
                "PandaProtocolAdapter: suppressed %d further '%s' error(s)",
                self._suppressed_error_count,
                self._last_error_signature,
            )
        self._last_error_signature = bucket
        self._suppressed_error_count = 0
        self._log.warning(
            "PandaProtocolAdapter: client error (%s): %s", bucket, text
        )

    def _reset_error_log_state(self, reason):
        """Reset the dedup state after a successful connect."""
        # A successful connect means any pending expected-close never
        # surfaced as an error (e.g. the socket reopened cleanly). Drop the
        # flag so it cannot mislabel a future genuine failure.
        self._expecting_close = False
        if self._suppressed_error_count > 0:
            self._log.info(
                "PandaProtocolAdapter: %s after %d suppressed error(s) "
                "('%s')",
                reason,
                self._suppressed_error_count,
                self._last_error_signature,
            )
        self._last_error_signature = None
        self._suppressed_error_count = 0

    def _close_active_socket(self):
        with self._send_lock:
            sock = self._active_socket
            self._active_socket = None
        if sock is None:
            return
        close = getattr(sock, "close", None)
        if not callable(close):
            return
        try:
            result = close()  # pylint: disable=not-callable
            if self._active_loop and asyncio is not None \
                    and asyncio.iscoroutine(result):
                asyncio.run_coroutine_threadsafe(result, self._active_loop)
        except Exception:
            self._log.debug(
                "PandaProtocolAdapter: close raised", exc_info=True
            )

    def _set_connected(self, value):
        if self._connected == value:
            return
        # Drop the stale RX timestamp on disconnect so the next connect
        # cycle doesn't carry an old "last seen" value that would trip
        # the controller's staleness watchdog the moment the socket
        # comes back up.
        if not value:
            self._last_rx = 0.0
        self._connected = value
        try:
            self._on_connection_change(value)
        except Exception:
            self._log.exception(
                "PandaProtocolAdapter: on_connection_change failed"
            )
