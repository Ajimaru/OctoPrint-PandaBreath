"""
MQTT control/telemetry bridge for Panda Breath firmware V1.0.4+.

Background and the full reverse-engineered protocol live in
``.ideas/ARCHITECTURE_mqtt_control.md`` and
``.ideas/captures/MQTT_HomeAssistant_v1.0.4.md``. Short version:

* The Panda (firmware V1.0.4+) is itself an MQTT client. When bound to a
  broker via its "Bind a Broker" WebUI menu it publishes its state to
  ``panda_breath/<ID>/state`` (~2 Hz) and accepts JSON commands on
  ``panda_breath/<ID>/command``. All nine writable fields were verified
  end-to-end.
* This bridge connects to the *same* broker as a second client. It:
    - learns the device ``<ID>`` from the state topic,
    - **reads** device state from ``panda_breath/<ID>/state``,
    - **sends** control commands to ``panda_breath/<ID>/command``,
    - **publishes** the plugin's own controller snapshot under a
      plugin-owned topic (``<base>/state``, default
      ``octoprint/pandabreath``) — Option B in the architecture doc, so we
      never squat on or duplicate the device's native HA discovery.

Why MQTT at all: the Panda's WebSocket does not ACK TX frames, forcing a
reconnect after every change. MQTT acknowledges delivery and streams state,
so day-to-day control avoids that reconnect churn. Safety (e-stop,
watchdog) and setup stay on the WebSocket — this bridge is control +
telemetry only.

paho-mqtt is a required runtime dependency and pinned to v2 because v1 is
unmaintained and uses an incompatible callback API.
"""

import json
import logging
import threading

try:
    import paho.mqtt.client as mqtt  # type: ignore[import-not-found]

    # We pin paho v2 (see pyproject) which changed the callback API; guard
    # the version too so a stray v1 install fails loudly rather than at the
    # first callback.
    _HAVE_PAHO = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:  # pragma: no cover - exercised via the import-guard test
    mqtt = None
    _HAVE_PAHO = False


# The MQTT ``mode`` enum uses display strings that differ from the
# WebSocket / ChamberController mode codes. Confirmed on write (not just
# read) against a real V1.0.4 device.
_WS_MODE_TO_MQTT = {
    "auto": "auto mode",
    "manual": "power on",
    "dry": "filament drying",
}
_MQTT_MODE_TO_WS = {v: k for k, v in _WS_MODE_TO_MQTT.items()}


def paho_available():
    """Return True if a compatible paho-mqtt (v2) is importable."""
    return _HAVE_PAHO


class MqttBridge:
    """
    Bridges a ChamberController to an MQTT broker.

    Lifecycle mirrors the protocol adapter: construct, :meth:`start`,
    :meth:`stop`. Safe to stop more than once. All broker I/O happens on
    paho's own network thread (``loop_start``); the only shared mutable
    state is the discovered device id, guarded by a lock.

    ``command_handler`` is called as ``command_handler(action, params)``
    for each inbound control message, where ``action`` is one of the
    ChamberController-facing verbs (``set_target``, ``set_mode``,
    ``set_heater``, ``set_custom_dry``, ``set_filter_threshold``,
    ``set_heater_threshold``, ``start_drying``, ``stop_drying``). The plugin
    routes these through the same validated dispatch the HTTP API uses, so
    range/lock/observe-only checks all still apply. The bridge deliberately
    never maps an e-stop: that stays on the WebSocket.
    """

    # Native device topics (read state / send commands).
    _DEVICE_STATE_WILDCARD = "panda_breath/+/state"

    def __init__(
        self,
        host,
        port=1883,
        username=None,
        password=None,
        base_topic="octoprint/pandabreath",
        command_handler=None,
        allow_control=True,
        logger=None,
        client_factory=None,
    ):
        if not paho_available() and client_factory is None:
            raise RuntimeError("paho-mqtt v2 not installed")
        self._host = host
        self._port = int(port)
        self._username = username or None
        self._password = password or None
        self._base_topic = base_topic.rstrip("/")
        self._command_handler = command_handler
        self._allow_control = bool(allow_control)
        self._log = logger or logging.getLogger(__name__)

        self._lock = threading.Lock()
        self._device_id = None
        self._started = False

        # ``client_factory`` lets tests inject a fake client; production
        # builds the real paho client.
        if client_factory is not None:
            self._client = client_factory()
        else:
            # Reached only when paho_available() is True (enforced in the
            # guard above), so ``mqtt`` is not None here — bind it to a
            # local so the static checker stops treating it as Optional.
            paho = mqtt
            # Type-narrowing only (mqtt is guaranteed non-None by the
            # paho_available() guard above); not a runtime safety check.
            # nosemgrep
            assert paho is not None
            self._client = paho.Client(
                callback_api_version=paho.CallbackAPIVersion.VERSION2
            )
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        # paho reconnects on its own between loop_start and disconnect; we
        # just register callbacks.
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ---- lifecycle --------------------------------------------------

    def start(self):
        """Connect (async) and start the network loop. Idempotent."""
        if self._started:
            return
        self._started = True
        # connect_async + loop_start gives us automatic reconnect handling
        # without blocking the caller (plugin startup thread).
        self._client.connect_async(self._host, self._port)
        self._client.loop_start()
        self._log.info(
            "MqttBridge: connecting to %s:%s (control=%s)",
            self._host,
            self._port,
            self._allow_control,
        )

    def stop(self):
        """Stop the loop and disconnect. Idempotent, never raises."""
        if not self._started:
            return
        self._started = False
        try:
            self._client.loop_stop()
        except Exception:  # pylint: disable=broad-exception-caught
            self._log.debug("MqttBridge: loop_stop raised", exc_info=True)
        try:
            self._client.disconnect()
        except Exception:  # pylint: disable=broad-exception-caught
            self._log.debug("MqttBridge: disconnect raised", exc_info=True)
        self._log.info("MqttBridge: stopped")

    # ---- outbound: publish the plugin's controller snapshot ---------

    def publish_state(self, snapshot):
        """
        Publish the controller snapshot to the plugin-owned state topic.

        Wired as a ChamberController listener; called on every state
        change. Best-effort: a publish failure (broker down) must not
        disturb the controller, so we swallow and log.
        """
        topic = self._base_topic + "/state"
        try:
            payload = json.dumps(snapshot, default=str)
            self._client.publish(topic, payload, qos=0, retain=True)
        except Exception:  # pylint: disable=broad-exception-caught
            self._log.debug("MqttBridge: publish_state failed", exc_info=True)

    # ---- outbound: send a control command to the device -------------

    def send_device_command(self, payload):
        """
        Publish a raw JSON command dict to the device command topic.

        ``payload`` is a dict using the device's own MQTT field names
        (e.g. ``{"target_temp": 45}``). No-op until the device id is known.
        """
        device_id = self.device_id()
        if device_id is None:
            self._log.debug(
                "MqttBridge: device id unknown, dropping command %r", payload
            )
            return False
        topic = f"panda_breath/{device_id}/command"
        try:
            self._client.publish(topic, json.dumps(payload), qos=1)
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            self._log.debug("MqttBridge: device command failed", exc_info=True)
            return False

    # ---- control sink: WS verb -> device MQTT command ---------------

    def control_sink(self, verb, **params):
        """
        Translate a ChamberController verb to a device MQTT command.

        Installed on the controller via ``set_control_sink``. Returns True
        if the command was translated and published to the device's native
        command topic; False to let the controller fall back to the
        WebSocket (for verbs without a clean MQTT equivalent, or before the
        device id is known).

        Never handles ``emergency_stop`` — that is not routed through the
        sink at all (safety stays on the WebSocket).
        """
        payload = self._verb_to_device_payload(verb, params)
        if payload is None:
            return False
        if payload == {}:
            # Recognised but intentionally a no-op over MQTT (e.g.
            # commit_dry — MQTT applies number writes immediately). Report
            # handled so we don't double-send over the WebSocket.
            return True
        return self.send_device_command(payload)

    def _verb_to_device_payload(self, verb, params):
        """
        Map (verb, params) to a device MQTT command dict.

        Returns None for verbs with no MQTT equivalent (caller falls back
        to WS), or ``{}`` for recognised no-ops.
        """
        if verb == "set_target":
            return {"target_temp": params.get("value")}
        if verb == "set_mode":
            mqtt_mode = self.ws_mode_to_mqtt(params.get("mode"))
            return {"mode": mqtt_mode} if mqtt_mode else None
        if verb == "set_filter_threshold":
            return {"filter_temp": params.get("value")}
        if verb == "set_heater_threshold":
            return {"heater_temp": params.get("value")}
        if verb == "set_dry_target":
            return {"custom_temp": params.get("value")}
        if verb == "set_dry_timer":
            return {"custom_timer": params.get("hours")}
        if verb == "commit_dry":
            return {}  # MQTT number writes apply immediately
        if verb == "heater_on":
            return {"work_on": "ON"}
        if verb == "heater_off":
            return {"work_on": "OFF"}
        if verb == "start_drying":
            return {"drying_running": "ON"}
        if verb == "stop_drying":
            return {"drying_running": "OFF"}
        if verb == "preset_pla":
            return {"filament_drying_mode": "pla"}
        if verb == "preset_petg":
            return {"filament_drying_mode": "petg"}
        # scan_printers, refresh_settings, etc. — WS only.
        return None

    # ---- discovery --------------------------------------------------

    def device_id(self):
        """Return the discovered device id, or None if not yet seen."""
        with self._lock:
            return self._device_id

    # ---- paho callbacks ---------------------------------------------

    def _on_connect(  # pylint: disable=unused-argument
        self, client, userdata, flags, reason_code, properties=None
    ):
        # paho v2 signature (args fixed by the callback contract).
        # Subscribe to the device state (to learn the id
        # and mirror state) and, if control is enabled, to our own command
        # topic so external clients (HA, scripts) can drive the chamber via
        # the plugin.
        if getattr(reason_code, "is_failure", False):
            self._log.warning("MqttBridge: connect failed: %s", reason_code)
            return
        client.subscribe(self._DEVICE_STATE_WILDCARD, qos=0)
        if self._allow_control:
            client.subscribe(self._base_topic + "/command", qos=1)
        self._log.info("MqttBridge: connected, subscriptions active")

    def _on_message(self, client, userdata, msg):  # pylint: disable=W0613
        topic = msg.topic
        if topic.endswith("/state") and topic.startswith("panda_breath/"):
            # Learn the device id from the native state topic once.
            parts = topic.split("/")
            if len(parts) >= 3:
                with self._lock:
                    if self._device_id != parts[1]:
                        self._device_id = parts[1]
                        self._log.info("MqttBridge: discovered device id %s", parts[1])
            return
        if topic == self._base_topic + "/command":
            self._handle_inbound_command(msg.payload)

    # ---- inbound command handling -----------------------------------

    def _handle_inbound_command(self, raw):
        """
        Parse an inbound command and route it through the handler.

        Accepts the plugin's controller-facing verb form::

            {"action": "set_target", "value": 45}
            {"action": "set_mode", "mode": "auto"}

        Unknown / malformed payloads are logged and ignored — never raised,
        since this runs on paho's network thread.
        """
        if not self._allow_control or self._command_handler is None:
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            self._log.warning("MqttBridge: undecodable command payload")
            return
        if not isinstance(data, dict):
            self._log.warning("MqttBridge: command payload not an object")
            return
        action = data.get("action")
        if not action:
            self._log.warning("MqttBridge: command missing 'action'")
            return
        try:
            self._command_handler(action, data)
        except Exception:  # pylint: disable=broad-exception-caught
            # The handler is the plugin's validated dispatch; it may raise
            # ValueError/PermissionError for bad input or lock/observe-only.
            # On the network thread we only log — there is no client to
            # return an HTTP status to.
            self._log.warning(
                "MqttBridge: command '%s' rejected", action, exc_info=True
            )

    # ---- helpers ----------------------------------------------------

    @staticmethod
    def ws_mode_to_mqtt(mode):
        """Map a ChamberController mode code to the device MQTT string."""
        return _WS_MODE_TO_MQTT.get(mode)

    @staticmethod
    def mqtt_mode_to_ws(mode):
        """Map a device MQTT mode string back to a controller mode code."""
        return _MQTT_MODE_TO_WS.get(mode)
