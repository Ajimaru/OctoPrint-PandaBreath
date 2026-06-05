#
# This file contains no material derived from third-party projects.
# Derived material — and the corresponding MIT attribution headers —
# lives in the sibling modules protocol.py and controller.py, which
# port frame layouts and field names from:
#
#   - https://github.com/jeng37/BIQU-Panda-Breath-Mod  (MIT)
#   - https://github.com/bula87/chamber_control         (MIT)
#
# Full upstream LICENSE texts are reproduced under licenses/ in the
# repository root (BIQU-Panda-Breath-Mod-LICENSE, chamber_control-LICENSE).
"""OctoPrint plugin: direct control of the BIQU Panda Breath chamber heater."""

import logging
import os
import re
import threading
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import urlopen

import flask
import octoprint.plugin
from flask_babel import gettext
from octoprint.access import ADMIN_GROUP
from octoprint.access.permissions import Permissions
from octoprint.events import Events
from octoprint.util import RepeatedTimer

from ._version import VERSION as PLUGIN_VERSION
from .controller import MODE_AUTO, ChamberController
from .frame_log import FrameLog
from .mqtt_bridge import MqttBridge, paho_available
from .protocol import PandaProtocolAdapter

# Minimum device firmware that exposes the MQTT control interface.
MQTT_MIN_FW = (1, 0, 4)

# A printer heater (bed/hotend) at or above this actual temperature counts
# as "still hot" for the drying interlock, even with no target set — so a
# dry cycle cannot be armed while the printer is heating up or cooling down.
PRINTER_HOT_THRESHOLD_C = 50.0


def _parse_fw_version(raw):
    """
    Parse a 'V1.0.4'/'1.0.4' string into a (1, 0, 4) tuple, or None.

    Numeric tuple compare avoids the string-compare trap where
    'V1.0.10' < 'V1.0.4'.
    """
    if not raw:
        return None
    s = str(raw).strip().lstrip("vV")
    parts = s.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except (ValueError, TypeError):
        return None


def fw_supports_mqtt(raw):
    """
    True if the firmware string is known and >= MQTT_MIN_FW.

    Returns False for unknown (None) versions — callers must distinguish
    'too old' from 'not yet known' themselves where it matters; for the
    hard gate on bridge start, unknown means 'do not start yet'.
    """
    parsed = _parse_fw_version(raw)
    if parsed is None:
        return False
    # Pad to 3 components for a clean tuple compare.
    parsed = parsed + (0,) * (3 - len(parsed))
    return parsed >= MQTT_MIN_FW


if TYPE_CHECKING:
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager


# Permission keys. Resolved at runtime as Permissions.PLUGIN_PANDABREATH_*.
PERMISSION_STATUS = "STATUS"
PERMISSION_CONTROL = "CONTROL"
PERMISSION_ADMIN = "ADMIN"


def _plugin_permission(key):
    return getattr(Permissions, "PLUGIN_PANDABREATH_" + key, None)


def _safe_error_message(exc):
    """
    Reduce an exception to a single short line safe to return to a client.

    The controller raises ``ValueError``/``PermissionError`` with our own,
    deliberately user-facing validation strings (e.g. ``"observe-only mode"``,
    ``"target exceeds max 60.0"``). Passing the full ``str(exc)`` straight into
    an HTTP response would, for any other exception, risk leaking internal
    detail (CodeQL: "Information exposure through an exception"). Take only the
    first line and cap the length so nothing beyond the intended message —
    multi-line stack/repr text — can reach the user.
    """
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return text[:200]


class PandabreathPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    """
    Wire the Panda Breath protocol adapter and chamber controller into OctoPrint.

    Hooks into OctoPrint's lifecycle, settings, API, event bus and
    GCODE-queuing hook.
    """

    # OctoPrint injects these attributes after construction (see
    # ``Plugin.__init__`` in ``octoprint.plugin.core``, which sets them to
    # ``None`` placeholders). Redeclaring them here under ``TYPE_CHECKING``
    # tells Pylance the real runtime types without changing behaviour.
    if TYPE_CHECKING:
        _logger: logging.Logger
        _settings: "PluginSettings"
        _plugin_manager: "PluginManager"
        _printer: object
        _identifier: str
        _plugin_name: str
        _plugin_version: str
        _basefolder: str

    def __init__(self):
        """Initialise plugin state; OctoPrint injects framework attributes later."""
        super().__init__()
        self._adapter = None
        self._controller = None
        self._watchdog = None
        self._frame_log = None  # type: FrameLog | None
        self._mqtt_bridge = None  # type: MqttBridge | None
        self._stack_lock = threading.Lock()
        # Throttle frame broadcasts: an idle Panda emits status frames
        # every few seconds, but a chatty bind sequence can burst. Cap
        # the per-second push rate so a busy plugin-message channel
        # cannot starve other plugins.
        self._frame_push_lock = threading.Lock()
        self._frame_push_last = 0.0
        self._latest_fw_version = None  # e.g. "V1.0.4"
        self._latest_fw_url = None  # link to firmware docs on GitHub

    # ---- SettingsPlugin ---------------------------------------------

    def get_settings_defaults(self):
        """Return defaults for every settings key the plugin reads."""
        return {
            # The Panda exposes its own ws server at ws:<ip>/ws — client
            # mode talks to it directly. Server mode is for the Bambu-
            # emulation style setups from BIQU-Panda-Breath-Mod.
            "transport": "client",
            "bind_host": "127.0.0.1",
            "bind_port": 8765,
            # Host/IP of the Panda device. The transport scheme (ws: vs
            # wss) and the /ws suffix are derived from tls_enabled at
            # connect time — the user only needs to enter the address.
            "client_host": "",
            "host_ip": "",
            "serial_number": "",
            "access_code": "",
            # Observe-only is the safe default. The adapter connects,
            # binds and polls but suppresses every write frame, the
            # controller stops issuing its own heater on/off commands,
            # and the HTTP API rejects mutating commands with 423.
            # Disable this only after verifying real-hardware behaviour
            # from the logs.
            "observe_only": True,
            # TLS — only relevant if the device or your network policy
            # actually requires wss://. Leave disabled otherwise.
            "tls_enabled": False,
            "tls_ca_file": "",
            "tls_cert_file": "",
            "tls_key_file": "",
            "tls_insecure": False,
            "max_temp": 70.0,
            "timeout_seconds": 15.0,
            "reconnect_delay": 5.0,
            "gcode_integration": True,
            "auto_on_print_start": False,
            "auto_off_print_end": True,
            "print_start_target": 40.0,
            # Show the emergency-stop button in the OctoPrint navbar.
            # Enabled by default — the operator can hide it from settings.
            "navbar_estop_enabled": True,
            # Expose the frame-history debug panel in the sidebar. Off by
            # default — only operators troubleshooting protocol issues
            # need it.
            "debug_panel_enabled": False,
            # Persistent WebSocket frame log on disk. Off by default — the
            # in-memory ring buffer is enough for live inspection. Turn this
            # on when you need a longer-running capture for reverse
            # engineering.
            "frame_log_enabled": False,
            # Days of frame-log files to retain. Older files are removed on
            # plugin start and on the daily rollover.
            "frame_log_retention_days": 7,
            # ---- MQTT control bridge (firmware V1.0.4+) ----------------
            # Off by default. Only usable when the device reports firmware
            # V1.0.4 or newer (the MQTT interface does not exist before
            # that) — the UI greys the toggle out otherwise. Day-to-day
            # control then flows over MQTT (acknowledged, no reconnect);
            # the WebSocket stays the safety + setup backbone.
            "mqtt_enabled": False,
            "mqtt_host": "",
            "mqtt_port": 1883,
            "mqtt_username": "",
            "mqtt_password": "",
            # Plugin-owned topic namespace for the snapshot the plugin
            # publishes and the command topic it listens on. The device's
            # own native topics (panda_breath/<id>/...) are used separately
            # for reading state and sending control frames.
            "mqtt_base_topic": "octoprint/pandabreath",
            # Allow inbound commands on <base>/command to drive the chamber.
            # Off → the bridge is publish/telemetry only.
            "mqtt_allow_control": True,
        }

    def get_settings_restricted_paths(self):
        """Mark device-pairing and TLS settings as admin-only."""
        # access_code is the device pairing secret; serial_number identifies
        # the hardware; host_ip is operationally sensitive. TLS material
        # may leak filesystem layout if exposed. All admin-only.
        return {
            "admin": [
                ["access_code"],
                ["serial_number"],
                ["host_ip"],
                ["tls_ca_file"],
                ["tls_cert_file"],
                ["tls_key_file"],
                # MQTT broker credentials are secrets too.
                ["mqtt_host"],
                ["mqtt_username"],
                ["mqtt_password"],
            ]
        }

    def on_settings_save(self, data):
        """Persist the new settings and bounce the adapter/controller stack."""
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._logger.info("PandaBreath: settings changed — restarting adapter")
        # Do not block the HTTP worker on adapter teardown (websocket close
        # + thread join can take seconds).
        threading.Thread(
            target=self._restart_stack,
            name="PandaBreathRestart",
            daemon=True,
        ).start()

    # ---- StartupPlugin / ShutdownPlugin -----------------------------

    def on_after_startup(self):
        """Bring up the protocol stack and the safety watchdog."""
        self._refresh_frame_log()
        self._start_stack()
        self._start_watchdog()
        threading.Thread(target=self._fetch_latest_fw, daemon=True).start()

    def _fetch_latest_fw(self):
        """Fetch the latest firmware version from BTT docs."""
        url = (
            "https://raw.githubusercontent.com/bigtreetech/docs/master"
            "/docs/Panda_Breath.md"
        )
        try:
            # nosec B310: url is a fixed https:// literal above, not
            # user-controlled, so no file:/custom-scheme risk applies.
            # nosemgrep
            with urlopen(url, timeout=10) as resp:  # nosec B310
                text = resp.read().decode("utf-8")
        except (URLError, OSError) as exc:
            self._logger.warning(
                "PandaBreath: could not fetch latest firmware info: %s",
                exc,
            )
            return
        # First match after "## Firmware History" wins — newest entry.
        match = re.search(
            r"## Firmware History.*?### \[(V[\d.]+)\]\((https://[^\)]+)\)",
            text,
            re.DOTALL,
        )
        if match:
            self._latest_fw_version = match.group(1)
            self._latest_fw_url = (
                "https://github.com/bigtreetech/docs/blob/master/docs/"
                "Panda_Breath.md#firmware-history"
            )
            self._logger.info(
                "PandaBreath: latest firmware is %s (%s)",
                self._latest_fw_version,
                self._latest_fw_url,
            )
            # Push the now-known latest version to any connected frontend.
            self._send_plugin_message(
                {
                    "kind": "latest_fw",
                    "latest_fw_version": self._latest_fw_version,
                    "latest_fw_url": self._latest_fw_url,
                }
            )
        else:
            self._logger.warning(
                "PandaBreath: could not parse latest firmware from docs"
            )

    def on_shutdown(self):
        """Tear down the watchdog and protocol stack on OctoPrint shutdown."""
        self._stop_watchdog()
        self._stop_stack()
        if self._frame_log is not None:
            self._frame_log.close()
            self._frame_log = None

    # ---- AssetPlugin / TemplatePlugin -------------------------------

    def get_assets(self):
        """Static assets shipped with the plugin (JS/CSS/LESS)."""
        return {
            "js": ["js/pandabreath.js"],
            "css": ["css/pandabreath.css"],
            "less": ["less/pandabreath.less"],
        }

    def get_template_configs(self):
        """
        Declare sidebar, settings and navbar templates.

        The navbar entry is always registered; its visibility is bound to
        the ``navbar_estop_enabled`` setting via ``data_bind`` so toggling
        the option takes effect without an OctoPrint reload.
        """
        return [
            {
                "type": "tab",
                "name": "Panda Breath",
                "template": "pandabreath_tab.jinja2",
                "custom_bindings": True,
            },
            {
                "type": "sidebar",
                "name": "Panda Breath",
                "icon": "fire",
                "template": "pandabreath_sidebar.jinja2",
                "custom_bindings": True,
            },
            {"type": "settings", "custom_bindings": False},
            {
                "type": "navbar",
                "template": "pandabreath_navbar.jinja2",
                "custom_bindings": True,
                "classes": ["dropdown"],
                "data_bind": (
                    "visible: settings.settings.plugins.pandabreath"
                    ".navbar_estop_enabled"
                ),
            },
        ]

    # ---- Permissions hook -------------------------------------------

    # pylint: disable-next=unused-argument
    def get_additional_permissions(self, *args, **kwargs):
        """
        Register the plugin's STATUS / CONTROL / ADMIN permissions.

        ``*args, **kwargs`` are required by the
        ``octoprint.access.permissions`` hook contract — OctoPrint may
        pass forward-compat arguments here.
        """
        return [
            {
                "key": PERMISSION_STATUS,
                "name": gettext("View chamber status"),
                "description": gettext("Allows reading Panda Breath chamber state."),
                "default_groups": [ADMIN_GROUP],
                "roles": ["status"],
                "dangerous": False,
            },
            {
                "key": PERMISSION_CONTROL,
                "name": gettext("Control chamber heater"),
                "description": gettext(
                    "Allows changing target, mode and heater state."
                ),
                "default_groups": [ADMIN_GROUP],
                "roles": ["control"],
                "dangerous": True,
            },
            {
                "key": PERMISSION_ADMIN,
                "name": gettext("Administer chamber lock"),
                "description": gettext(
                    "Allows locking/unlocking the safety interlock."
                ),
                "default_groups": [ADMIN_GROUP],
                "roles": ["admin"],
                "dangerous": True,
            },
        ]

    # ---- SimpleApiPlugin --------------------------------------------

    def is_api_protected(self):
        """Require an authenticated user for every API call."""
        # All commands carry side-effects on a heater — never anonymous.
        return True

    def get_api_commands(self):
        """Declare the SimpleApiPlugin command set and required fields."""
        return {
            "set_target": ["value"],
            "set_mode": ["mode"],
            "set_heater": ["on"],
            "set_custom_dry": ["value", "hours"],
            "preset_pla": [],
            "preset_petg": [],
            "start_drying": [],
            "stop_drying": [],
            "set_filter_threshold": ["value"],
            "set_heater_threshold": ["value"],
            "scan_printers": [],
            "refresh_settings": [],
            "lock": [],
            "unlock": [],
            "emergency_stop": [],
        }

    def on_api_get(self, request):
        """Return the current controller snapshot (with optional frames)."""
        perm = _plugin_permission(PERMISSION_STATUS)
        if perm is not None and not perm.can():
            return flask.abort(403)
        if self._controller is None:
            return flask.jsonify({"available": False})
        snapshot = self._controller.snapshot()
        snapshot["available"] = True
        snapshot["latest_fw_version"] = self._latest_fw_version
        snapshot["latest_fw_url"] = self._latest_fw_url
        self._decorate_mqtt_status(snapshot)
        # Always attach the full history on REST GET so the tab can
        # populate the chart on first paint. Push messages send only the
        # incremental sample to keep the channel cheap.
        snapshot["history"] = self._controller.history_samples()
        # ?debug=1 attaches the frame ring buffer for the debug panel.
        if request.values.get("debug") in ("1", "true", "yes"):
            snapshot["frames"] = (
                self._adapter.get_frame_history() if self._adapter is not None else []
            )
            snapshot["frame_log"] = self._frame_log_status()
        return flask.jsonify(snapshot)

    def _frame_log_status(self):
        """Return a UI-friendly summary of on-disk frame logs."""
        log = self._frame_log
        if log is None:
            return {"enabled": False, "files": []}
        files = [
            {"name": name, "size": size, "mtime": mtime}
            for name, size, mtime in log.list_files()
        ]
        return {
            "enabled": True,
            "directory": log.directory(),
            "files": files,
        }

    # ---- BlueprintPlugin (frame log download) -----------------------

    def is_blueprint_protected(self):
        """All blueprint routes require an authenticated user."""
        return True

    @octoprint.plugin.BlueprintPlugin.route("/frame_logs", methods=["GET"])
    def blueprint_list_frame_logs(self):
        """Return the current frame-log file list as JSON."""
        perm = _plugin_permission(PERMISSION_STATUS)
        if perm is not None and not perm.can():
            return flask.abort(403)
        return flask.jsonify(self._frame_log_status())

    @octoprint.plugin.BlueprintPlugin.route(
        "/frame_logs/<string:filename>", methods=["GET"]
    )
    def blueprint_download_frame_log(self, filename):
        """Download a specific frame-log file."""
        perm = _plugin_permission(PERMISSION_STATUS)
        if perm is not None and not perm.can():
            return flask.abort(403)
        log = self._frame_log
        if log is None:
            return flask.abort(404)
        path = log.path_for(filename)
        if path is None or not os.path.isfile(path):
            return flask.abort(404)
        # ``send_from_directory`` re-checks the path stays inside the
        # directory, defence-in-depth on top of ``path_for``.
        return flask.send_from_directory(
            log.directory(),
            filename,
            as_attachment=True,
            mimetype="application/x-ndjson",
        )

    @octoprint.plugin.BlueprintPlugin.route(
        "/frame_logs/<string:filename>", methods=["DELETE"]
    )
    def blueprint_delete_frame_log(self, filename):
        """Delete a specific frame-log file."""
        perm = _plugin_permission(PERMISSION_ADMIN)
        if perm is not None and not perm.can():
            return flask.abort(403)
        log = self._frame_log
        if log is None:
            return flask.abort(404)
        path = log.path_for(filename)
        if path is None or not os.path.isfile(path):
            return flask.abort(404)
        try:
            os.remove(path)
        except OSError:
            # Don't leak filesystem details (paths, errno text) to the client;
            # the full error goes to the server log instead.
            self._logger.exception(
                "PandaBreath: failed to delete frame log '%s'", filename
            )
            return flask.make_response(
                flask.jsonify({"error": "could not delete frame log"}),
                500,
            )
        return flask.jsonify(self._frame_log_status())

    @octoprint.plugin.BlueprintPlugin.route("/frame_logs", methods=["DELETE"])
    def blueprint_delete_all_frame_logs(self):
        """Delete every frame-log file in the data directory."""
        perm = _plugin_permission(PERMISSION_ADMIN)
        if perm is not None and not perm.can():
            return flask.abort(403)
        log = self._frame_log
        if log is None:
            return flask.jsonify({"deleted": 0, "errors": []})
        deleted = 0
        errors = []
        # Close the current handle so the file we're about to remove
        # isn't held open while we unlink it (Windows would otherwise
        # refuse); ``write()`` reopens lazily on the next frame.
        try:
            log.close()
        except Exception:  # pylint: disable=broad-exception-caught
            self._logger.debug(
                "PandaBreath: frame log close before bulk delete failed",
                exc_info=True,
            )
        for name, _size, _mtime in log.list_files():
            path = log.path_for(name)
            if path is None or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                deleted += 1
            except OSError:
                # See blueprint_delete_frame_log: keep filesystem error
                # detail out of the response, log it server-side.
                self._logger.exception(
                    "PandaBreath: failed to delete frame log '%s'", name
                )
                errors.append({"name": name, "error": "could not delete"})
        return flask.jsonify(
            {
                "deleted": deleted,
                "errors": errors,
                "status": self._frame_log_status(),
            }
        )

    def on_api_command(self, command, data):
        """Dispatch a SimpleApiPlugin command to the chamber controller."""
        required = (
            PERMISSION_ADMIN
            if command in ("lock", "unlock", "emergency_stop")
            else PERMISSION_CONTROL
        )
        perm = _plugin_permission(required)
        if perm is not None and not perm.can():
            return flask.abort(403)

        if self._controller is None:
            return flask.make_response(
                flask.jsonify({"error": "PandaBreath not initialised"}),
                503,
            )
        # Drying interlock: the chamber's dry-mode is for drying filament,
        # not for printing. While the OctoPrint printer this Panda is paired
        # with is running a job (or starting/pausing/cancelling one), reject
        # the two commands that would arm a dry cycle. The frontend disables
        # the buttons too, but a direct API call must not bypass this.
        if command in ("start_drying",) or (
            command == "set_mode" and data.get("mode") == "dry"
        ):
            if self._printer_is_busy():
                return flask.make_response(
                    flask.jsonify(
                        {
                            "error": (
                                "Cannot start drying while the printer is "
                                "running a job or heating."
                            )
                        }
                    ),
                    409,
                )
        try:
            if not self._apply_control_command(command, data, source="api"):
                return flask.make_response(
                    flask.jsonify({"error": "unknown command"}), 400
                )
        except PermissionError as exc:
            # 409 for observe-only (configuration conflict), 423 for safety
            # lock (resource is locked). Both are HTTP-semantic correct.
            message = _safe_error_message(exc)
            status = 409 if "observe-only" in message else 423
            return flask.make_response(flask.jsonify({"error": message}), status)
        except (ValueError, TypeError) as exc:
            return flask.make_response(
                flask.jsonify({"error": _safe_error_message(exc)}), 400
            )
        return flask.jsonify(self._controller.snapshot())

    def _apply_control_command(self, command, data, source="api"):
        """
        Map a control verb onto the controller. Shared by HTTP + MQTT.

        Returns True if the command was recognised and dispatched, False
        for an unknown command. Raises ``ValueError``/``TypeError`` for bad
        input and ``PermissionError`` for lock/observe-only — callers
        translate those into their own error responses. Requires
        ``self._controller`` to be set (checked by callers).

        ``emergency_stop`` is intentionally reachable here, but the MQTT
        bridge never forwards it (e-stop stays on the WebSocket); only the
        HTTP navbar button does.
        """
        c = self._controller
        if c is None:
            # Stack torn down between the caller's check and here (settings
            # restart). Treat as not-dispatched; callers already guard too.
            return False
        if command == "set_target":
            c.set_target(data.get("value"))
        elif command == "set_mode":
            c.set_mode(data.get("mode"))
        elif command == "set_heater":
            c.set_heater(bool(data.get("on")))
        elif command == "set_custom_dry":
            c.set_custom_dry(data.get("value"), data.get("hours"))
        elif command == "preset_pla":
            c.select_preset_pla()
        elif command == "preset_petg":
            c.select_preset_petg()
        elif command == "start_drying":
            c.start_drying()
        elif command == "stop_drying":
            c.stop_drying()
        elif command == "set_filter_threshold":
            c.set_filter_threshold(data.get("value"))
        elif command == "set_heater_threshold":
            c.set_heater_threshold(data.get("value"))
        elif command == "scan_printers":
            c.scan_printers()
        elif command == "refresh_settings":
            c.refresh_settings()
        elif command == "lock":
            c.lock(reason=source)
        elif command == "unlock":
            c.unlock()
        elif command == "emergency_stop":
            self._logger.warning("PandaBreath: EMERGENCY STOP triggered via %s", source)
            c.emergency_stop(reason="navbar_estop")
        else:
            return False
        return True

    def _on_mqtt_command(self, action, data):
        """
        Inbound MQTT command handler passed to MqttBridge.

        Runs on paho's network thread. Routes through the same validated
        dispatch as the HTTP API so ranges, lock and observe-only all
        apply. Refuses e-stop over MQTT (safety stays on the WebSocket)
        and the dry interlock while a print job is busy.
        """
        if self._controller is None:
            return
        if action == "emergency_stop":
            self._logger.warning(
                "PandaBreath: refused emergency_stop over MQTT "
                "(use the WebSocket/navbar)"
            )
            return
        if action in ("start_drying",) or (
            action == "set_mode" and data.get("mode") == "dry"
        ):
            if self._printer_is_busy():
                self._logger.info(
                    "PandaBreath: MQTT dry command ignored — printer busy"
                )
                return
        if not self._apply_control_command(action, data, source="mqtt"):
            self._logger.warning("PandaBreath: unknown MQTT command '%s'", action)

    # ---- EventHandlerPlugin -----------------------------------------

    def on_event(self, event, payload):  # pylint: disable=unused-argument
        """React to print-lifecycle events for auto-on / auto-off."""
        # ``payload`` is part of the EventHandlerPlugin contract; we do not
        # use it but must accept it.
        if self._controller is None:
            return
        get_bool = self._settings.get_boolean
        if event == Events.PRINT_STARTED and get_bool(["auto_on_print_start"]):
            target = float(self._settings.get(["print_start_target"]))
            try:
                self._controller.set_mode(MODE_AUTO)
                self._controller.set_target(target)
            except (ValueError, PermissionError):
                # set_mode/set_target only raise these — anything else is
                # a bug we want to see in the traceback.
                self._logger.exception(
                    "PandaBreath: failed to apply auto-on at print start"
                )
        elif event in (
            Events.PRINT_DONE,
            Events.PRINT_FAILED,
            Events.PRINT_CANCELLED,
        ):
            if get_bool(["auto_off_print_end"]):
                try:
                    self._controller.set_target(0.0)
                    self._controller.set_heater(False)
                except (ValueError, PermissionError):
                    self._logger.exception(
                        "PandaBreath: failed to apply auto-off at print end"
                    )

    # ---- GCODE hook (M141 / M191) -----------------------------------

    def hook_gcode_queuing(  # pylint: disable=unused-argument
        self,
        comm_instance,
        phase,
        cmd,
        cmd_type,
        gcode,
        *args,
        **kwargs,
    ):
        """
        Intercept M141/M191 from the gcode stream and re-target the chamber.

        The full signature is dictated by OctoPrint's
        ``octoprint.comm.protocol.gcode.queuing`` hook contract; all positional
        arguments must be accepted even when we only consume ``cmd`` and
        ``gcode``. The optional ``subcode``/``tags`` keyword arguments are
        absorbed by ``**kwargs`` since this hook does not use them.
        """
        if self._controller is None:
            return None
        if not self._settings.get_boolean(["gcode_integration"]):
            return None
        if gcode not in ("M141", "M191"):
            return None
        target = self._parse_s_value(cmd)
        if target is None:
            return None
        try:
            self._controller.set_mode(MODE_AUTO)
            self._controller.set_target(target)
        except (ValueError, PermissionError):
            self._logger.exception("PandaBreath: failed to apply %s", gcode)
        # Swallow the command — OctoPrint would otherwise forward it to
        # the printer firmware, which usually does not handle M141/M191.
        return (None,)

    @staticmethod
    def _parse_s_value(cmd):
        for token in cmd.split():
            if token.startswith(("S", "s")):
                try:
                    return float(token[1:])
                except ValueError:
                    return None
        return None

    # ---- Softwareupdate hook ---------------------------------------

    def get_update_information(self):
        """Software-update hook payload for OctoPrint's update manager."""
        pip_url = (
            "https://github.com/ajimaru/"
            "OctoPrint-PandaBreath/archive/{target_version}.zip"
        )
        return {
            "pandabreath": {
                "displayName": "PandaBreath Plugin",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "ajimaru",
                "repo": "OctoPrint-PandaBreath",
                "current": self._plugin_version,
                "pip": pip_url,
            }
        }

    # ---- internals --------------------------------------------------

    def _printer_is_busy(self):
        """
        Return True while the OctoPrint printer is running a job OR heating.

        Two conditions block arming a dry cycle:

        * An active job — the whole window from print start (including the
          transient starting/resuming phases) through pausing/cancelling and
          a held pause.
        * The printer is heating or still hot — any bed/hotend heater with a
          target above 0 °C, or an actual temperature at/above
          ``PRINTER_HOT_THRESHOLD_C`` (covers manual preheat and cool-down,
          not just printing).

        Dry-mode is for drying filament; it must not run while the chamber is
        needed for a print or while the printer is hot. ``self._printer`` is
        injected by OctoPrint; guard defensively in case it isn't ready yet.
        """
        printer = getattr(self, "_printer", None)
        if printer is None:
            return False
        try:
            if (
                printer.is_printing()
                or printer.is_starting()
                or printer.is_pausing()
                or printer.is_paused()
                or printer.is_resuming()
                or printer.is_cancelling()
            ):
                return True
        except Exception:  # pylint: disable=broad-exception-caught
            # The PrinterInterface predicates should never raise, but a
            # missing/older method must not break command dispatch — treat
            # an unknown state as not-busy so we fail open on availability
            # (the frontend gate and the operator remain the backstop).
            self._logger.debug("PandaBreath: printer-busy probe failed", exc_info=True)
        return self._printer_is_heating()

    def _printer_is_heating(self):
        """
        Return True if any printer heater has a target set or is hot.

        Reads ``get_current_temperatures()`` and inspects the bed and hotend
        (``tool*``) entries. The Panda's own ``chamber`` entry is ignored —
        only the paired printer's heaters gate drying. Fails open (returns
        False) if temperatures are unavailable.
        """
        printer = getattr(self, "_printer", None)
        if printer is None:
            return False
        try:
            temps = printer.get_current_temperatures() or {}
        except Exception:  # pylint: disable=broad-exception-caught
            self._logger.debug("PandaBreath: temperature probe failed", exc_info=True)
            return False
        for name, entry in temps.items():
            # Only the paired printer's heaters count; skip the Panda chamber.
            if name == "chamber" or not isinstance(entry, dict):
                continue
            target = entry.get("target") or 0
            actual = entry.get("actual") or 0
            try:
                if float(target) > 0 or float(actual) >= PRINTER_HOT_THRESHOLD_C:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _build_client_url(host, tls_enabled):
        """
        Assemble a ws: or wss: URL from a bare host or partial URL.

        Accepts plain IPs, host:port pairs, and full URLs — anything the
        user might paste. The scheme is forced to match tls_enabled, and
        a /ws path is appended when none is given.
        """
        host = host.strip()
        scheme = "wss" if tls_enabled else "ws"
        # Strip any existing ws, wss, http, https scheme prefix so we can
        # re-attach the scheme that matches the current TLS toggle.
        # nosemgrep
        for prefix in ("wss://", "ws://", "https://", "http://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix) :]
                break
        # Separate path so we don't double the suffix.
        path = "/ws"
        if "/" in host:
            host, _, tail = host.partition("/")
            if tail:
                path = "/" + tail
        return f"{scheme}://{host}{path}"

    def _start_stack(self):
        with self._stack_lock:
            s = self._settings
            transport = s.get(["transport"]) or "client"
            client_host = (s.get(["client_host"]) or "").strip()
            tls_enabled = s.get_boolean(["tls_enabled"])
            # In client mode an empty host means the user has not configured
            # the device yet — starting the adapter would spam reconnect
            # warnings into the log every few seconds. Stay idle until the
            # host is filled in (on_settings_save will restart the stack).
            if transport == "client" and not client_host:
                self._logger.info(
                    "PandaBreath: client_host not set — adapter idle "
                    "until configured"
                )
                return
            client_url = self._build_client_url(client_host, tls_enabled)
            adapter = PandaProtocolAdapter(
                mode=transport,
                host=s.get(["bind_host"]),
                port=int(s.get(["bind_port"])),
                client_url=client_url,
                serial_number=s.get(["serial_number"]) or None,
                access_code=s.get(["access_code"]) or None,
                host_ip=s.get(["host_ip"]) or None,
                on_status=self._on_status,
                on_connection_change=self._on_connection_change,
                on_frame=self._on_frame,
                on_frame_persist=self._on_frame_persist,
                logger=self._logger,
                reconnect_delay=float(s.get(["reconnect_delay"])),
                observe_only=s.get_boolean(["observe_only"]),
                debug_enabled_getter=lambda: self._settings.get_boolean(
                    ["debug_panel_enabled"]
                ),
                tls_enabled=s.get_boolean(["tls_enabled"]),
                tls_ca_file=s.get(["tls_ca_file"]) or None,
                tls_cert_file=s.get(["tls_cert_file"]) or None,
                tls_key_file=s.get(["tls_key_file"]) or None,
                tls_insecure=s.get_boolean(["tls_insecure"]),
            )
            controller = ChamberController(
                adapter=adapter,
                max_temp=float(s.get(["max_temp"])),
                timeout_seconds=float(s.get(["timeout_seconds"])),
                logger=self._logger,
            )
            controller.add_listener(self._push_status)
            self._adapter = adapter
            self._controller = controller
            adapter.start()
            self._logger.info(
                "PandaBreath: stack started (transport=%s, bind=%s:%s, "
                "observe_only=%s)",
                transport,
                s.get(["bind_host"]),
                s.get(["bind_port"]),
                s.get_boolean(["observe_only"]),
            )
        # MQTT bridge is started outside the stack lock: it has its own
        # network thread and must not hold the lock while connecting.
        self._start_mqtt_bridge()

    def _start_mqtt_bridge(self):
        """
        Bring up the MQTT bridge if enabled and fw-supported.

        Deliberately gated three ways:
        * setting ``mqtt_enabled`` on,
        * paho-mqtt importable,
        * device firmware known and >= V1.0.4.

        The firmware is read from the controller snapshot. If it is not yet
        known (WebSocket still connecting) we skip and rely on the next
        settings save / reconnect to retry, rather than starting a bridge
        the device cannot serve.
        """
        s = self._settings
        if not s.get_boolean(["mqtt_enabled"]):
            return
        if self._mqtt_bridge is not None:
            return
        if not paho_available():
            self._logger.warning(
                "PandaBreath: mqtt_enabled but paho-mqtt not installed — "
                "bridge inactive."
            )
            return
        host = (s.get(["mqtt_host"]) or "").strip()
        if not host:
            self._logger.warning(
                "PandaBreath: mqtt_enabled but no broker host set — bridge inactive"
            )
            return
        fw = None
        if self._controller is not None:
            fw = self._controller.snapshot().get("fw_version")
        if fw is None:
            self._logger.info(
                "PandaBreath: mqtt_enabled and broker configured, waiting "
                "for first device status frame to confirm firmware before "
                "starting MQTT bridge"
            )
            return
        if not fw_supports_mqtt(fw):
            self._logger.warning(
                "PandaBreath: MQTT requires firmware V1.0.4+ — device "
                "reports %r; bridge not started",
                fw,
            )
            return
        try:
            bridge = MqttBridge(
                host=host,
                port=int(s.get(["mqtt_port"]) or 1883),
                username=s.get(["mqtt_username"]) or None,
                password=s.get(["mqtt_password"]) or None,
                base_topic=(s.get(["mqtt_base_topic"]) or "octoprint/pandabreath"),
                command_handler=self._on_mqtt_command,
                allow_control=s.get_boolean(["mqtt_allow_control"]),
                logger=self._logger,
            )
            bridge.start()
            if self._controller is not None:
                self._controller.add_listener(bridge.publish_state)
                # Route operational commands over MQTT (acknowledged, no
                # WS reconnect). Safety frames stay on the WebSocket — the
                # controller never sends those through the sink.
                self._controller.set_control_sink(bridge.control_sink)
            self._mqtt_bridge = bridge
            self._logger.info("PandaBreath: MQTT bridge started (control over MQTT)")
        except Exception:  # pylint: disable=broad-exception-caught
            self._logger.exception("PandaBreath: failed to start MQTT bridge")
            self._mqtt_bridge = None

    def _stop_mqtt_bridge(self):
        bridge = self._mqtt_bridge
        self._mqtt_bridge = None
        # Detach the control sink first so any in-flight command falls back
        # to the WebSocket rather than a stopping bridge.
        if self._controller is not None:
            self._controller.set_control_sink(None)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:  # pylint: disable=broad-exception-caught
                self._logger.exception("PandaBreath: MQTT bridge stop failed")

    def _stop_stack(self):
        # Bridge first: it holds a controller-listener reference and its own
        # network thread; stop it before the controller goes away.
        self._stop_mqtt_bridge()
        with self._stack_lock:
            adapter = self._adapter
            self._adapter = None
            self._controller = None
        if adapter is not None:
            try:
                adapter.stop()
            except Exception:  # pylint: disable=broad-exception-caught
                # Teardown must not propagate: adapter.stop() drives socket
                # close + thread join, both of which can raise from a wide
                # set of backend libraries (websockets, ssl, OS errors).
                self._logger.exception("PandaBreath: adapter stop failed")

    def _restart_stack(self):
        self._refresh_frame_log()
        self._stop_stack()
        self._start_stack()

    def _refresh_frame_log(self):
        """
        Open / close / re-open the on-disk frame log to match settings.

        Called on startup and on every settings save so the user can
        toggle the disk capture without restarting OctoPrint.

        The debug panel is the master gate for all debug-only behaviour:
        disk capture only runs when the panel is enabled *and* the
        dedicated ``frame_log_enabled`` toggle is on. With the panel off,
        no frames are persisted regardless of that toggle.
        """
        enabled = self._settings.get_boolean(
            ["debug_panel_enabled"]
        ) and self._settings.get_boolean(["frame_log_enabled"])
        if not enabled:
            if self._frame_log is not None:
                self._frame_log.close()
                self._frame_log = None
            return
        retention = int(self._settings.get(["frame_log_retention_days"]) or 7)
        data_dir = self.get_plugin_data_folder()
        # Recreate only when first enabling — the writer handles its own
        # daily rotation and the retention cleanup runs on each rollover.
        if self._frame_log is None:
            self._frame_log = FrameLog(
                directory=data_dir,
                retention_days=retention,
                logger=self._logger,
            )
        else:
            # Retention may have changed — close + reopen is cheap.
            self._frame_log.close()
            self._frame_log = FrameLog(
                directory=data_dir,
                retention_days=retention,
                logger=self._logger,
            )

    def _on_status(self, payload):
        if self._controller is not None:
            self._controller.on_status(payload)

    def _on_connection_change(self, connected):
        state = "connected" if connected else "disconnected"
        self._logger.info("PandaBreath: peer %s", state)
        if self._controller is not None:
            self._push_status(self._controller.snapshot())

    def _decorate_mqtt_status(self, snapshot):
        """Attach MQTT gate/state fields to a snapshot. Shared by GET+push."""
        snapshot["mqtt_enabled"] = self._settings.get_boolean(["mqtt_enabled"])
        snapshot["mqtt_active"] = self._mqtt_bridge is not None
        snapshot["mqtt_supported"] = fw_supports_mqtt(snapshot.get("fw_version"))

    def _push_status(self, snapshot):
        snapshot["latest_fw_version"] = self._latest_fw_version
        snapshot["latest_fw_url"] = self._latest_fw_url
        self._decorate_mqtt_status(snapshot)
        self._send_plugin_message({"kind": "status", "snapshot": snapshot})
        # Deferred bridge start: the firmware version only becomes known
        # once the WebSocket delivers a snapshot, which may be after
        # _start_stack ran. Retry here when the device now qualifies.
        if (
            self._mqtt_bridge is None
            and self._settings.get_boolean(["mqtt_enabled"])
            and fw_supports_mqtt(snapshot.get("fw_version"))
        ):
            self._start_mqtt_bridge()

    def _on_frame_persist(self, direction, frame):
        """Write the untruncated frame to the on-disk log if enabled."""
        log = self._frame_log
        if log is None:
            return
        try:
            log.write(direction, frame)
        except Exception:  # pylint: disable=broad-exception-caught
            self._logger.exception("PandaBreath: frame log write failed")

    def _on_frame(self, direction, frame):
        # Rate-limit to ~5 broadcasts/s. UI catches up via the debug API
        # on next refresh if we drop a burst.
        import time as _time

        now = _time.monotonic()
        with self._frame_push_lock:
            if now - self._frame_push_last < 0.2:
                return
            self._frame_push_last = now
        self._send_plugin_message(
            {
                "kind": "frame",
                "ts": _time.time(),
                "dir": direction,
                "frame": frame,
            }
        )

    def _send_plugin_message(self, payload):
        perm = _plugin_permission(PERMISSION_STATUS)
        try:
            if perm is not None:
                self._plugin_manager.send_plugin_message(
                    self._identifier, payload, permissions=[perm]
                )
            else:
                self._plugin_manager.send_plugin_message(self._identifier, payload)
        except Exception:  # pylint: disable=broad-exception-caught
            # OctoPrint's plugin_manager dispatches into the SockJS layer;
            # surfacing any failure there must not break controller updates.
            self._logger.exception("PandaBreath: failed to broadcast plugin message")

    # ---- watchdog ---------------------------------------------------

    def _start_watchdog(self):
        self._stop_watchdog()
        timer = RepeatedTimer(
            2.0,
            self._watchdog_tick,
            run_first=False,
            condition=lambda: self._controller is not None,
            on_condition_false=lambda: None,
        )
        timer.daemon = True
        timer.start()
        self._watchdog = timer

    def _stop_watchdog(self):
        timer = self._watchdog
        self._watchdog = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                # RepeatedTimer.cancel() touches threading internals; a
                # failure here must not block shutdown of the rest of the
                # stack.
                self._logger.exception("PandaBreath: watchdog cancel failed")

    def _watchdog_tick(self):
        controller = self._controller
        if controller is None:
            return
        try:
            controller.watchdog_tick()
        except Exception:  # pylint: disable=broad-exception-caught
            # The watchdog is a safety-critical timer: a single exception
            # must never silence subsequent ticks.
            self._logger.exception("PandaBreath: watchdog tick failed")


__plugin_name__ = "PandaBreath"
__plugin_description__ = (
    "Direct WebSocket control of the BIQU Panda Breath chamber heater."
)
__plugin_version__ = PLUGIN_VERSION
__plugin_license__ = "MIT"
__plugin_url__ = "https://github.com/ajimaru/OctoPrint-PandaBreath"
__plugin_author__ = "Ajimaru"
__plugin_pythoncompat__ = ">=3.9,<4"

__plugin_implementation__ = None
__plugin_hooks__ = None


def __plugin_load__():  # noqa: N807  (mandatory OctoPrint loader hook name)
    """OctoPrint plugin entry point — wires implementation and hooks."""
    # Set the module-level attributes that OctoPrint's plugin loader reads
    # after this function returns. Done via ``setattr`` on the module
    # object so the assignment is explicit and no ``global`` statement is
    # needed.
    import sys

    impl = PandabreathPlugin()
    module = sys.modules[__name__]
    setattr(module, "__plugin_implementation__", impl)
    setattr(
        module,
        "__plugin_hooks__",
        {
            "octoprint.plugin.softwareupdate.check_config": (
                impl.get_update_information
            ),
            "octoprint.comm.protocol.gcode.queuing": impl.hook_gcode_queuing,
            "octoprint.access.permissions": impl.get_additional_permissions,
        },
    )
