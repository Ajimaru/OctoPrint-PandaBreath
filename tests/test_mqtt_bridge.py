"""Unit tests for the MQTT control bridge and the firmware gate.

The bridge is exercised against a ``FakeMqttClient`` injected via
``client_factory`` so no real paho-mqtt or broker is needed. The firmware
gate helpers live in the plugin package ``__init__`` (imported through the
conftest OctoPrint stubs).
"""

import json
from typing import Callable, Optional

import pytest

from octoprint_pandabreath.mqtt_bridge import MqttBridge


class FakeMqttClient:
    """Records publishes/subscribes and lets tests drive callbacks."""

    def __init__(self):
        self.published = []  # (topic, payload, qos, retain)
        self.subscribed = []  # (topic, qos)
        self.username = None
        self.connected_async = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.on_connect: Optional[Callable[..., None]] = None
        self.on_message: Optional[Callable[..., None]] = None

    # paho-compatible surface used by MqttBridge
    def username_pw_set(self, user, password):
        """Record configured credentials."""
        self.username = (user, password)

    def connect_async(self, host, port):
        """Record async connect target."""
        self.connected_async = (host, port)

    def loop_start(self):
        """Record network loop start."""
        self.loop_started = True

    def loop_stop(self):
        """Record network loop stop."""
        self.loop_stopped = True

    def disconnect(self):
        """Record disconnect invocation."""
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        """Record subscribe calls."""
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        """Record publish calls."""
        self.published.append((topic, payload, qos, retain))

    # ---- test helpers ----

    def fire_connect(self, failure=False):
        """Trigger on_connect callback as paho would do."""
        rc = type("RC", (), {"is_failure": failure})()
        callback = self.on_connect
        if callback is not None:
            callback(self, None, {}, rc, None)

    def fire_message(self, topic, payload):
        """Trigger on_message callback with encoded payload."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        msg = type("Msg", (), {"topic": topic, "payload": payload})()
        callback = self.on_message
        if callback is not None:
            callback(self, None, msg)


def make_bridge(**kwargs):
    """Create a bridge bound to a fake MQTT client for deterministic tests."""
    client = FakeMqttClient()
    kwargs.setdefault("host", "broker.local")
    kwargs.setdefault("base_topic", "octoprint/pandabreath")
    bridge = MqttBridge(client_factory=lambda: client, **kwargs)
    return bridge, client


# ---- lifecycle ----------------------------------------------------------


def test_start_connects_and_subscribes():
    """Starting bridge connects and subscribes expected topics."""
    bridge, client = make_bridge()
    bridge.start()
    assert client.connected_async == ("broker.local", 1883)
    assert client.loop_started
    client.fire_connect()
    topics = [t for t, _ in client.subscribed]
    assert "panda_breath/+/state" in topics
    assert "octoprint/pandabreath/command" in topics


def test_start_is_idempotent():
    """Starting the bridge twice does not duplicate startup side effects."""
    bridge, client = make_bridge()
    bridge.start()
    bridge.start()
    # connect_async only meaningfully called once (loop_start once)
    assert client.loop_started


def test_stop_is_safe_and_idempotent():
    """Stopping is safe to call repeatedly and disconnects the client."""
    bridge, client = make_bridge()
    bridge.start()
    bridge.stop()
    bridge.stop()
    assert client.loop_stopped and client.disconnected


def test_telemetry_only_does_not_subscribe_command():
    """Control command topic is not subscribed when control is disabled."""
    bridge, client = make_bridge(allow_control=False)
    bridge.start()
    client.fire_connect()
    topics = [t for t, _ in client.subscribed]
    assert "octoprint/pandabreath/command" not in topics


# ---- device id discovery + outbound -------------------------------------


def test_device_id_learned_from_state_topic():
    """Device id is learned from incoming native state topic."""
    bridge, client = make_bridge()
    bridge.start()
    client.fire_connect()
    assert bridge.device_id() is None
    client.fire_message("panda_breath/ABC123/state", {"chamber_temp": 30})
    assert bridge.device_id() == "ABC123"


def test_send_device_command_uses_native_topic():
    """Outbound device commands publish to the native command topic."""
    bridge, client = make_bridge()
    bridge.start()
    client.fire_connect()
    client.fire_message("panda_breath/ABC123/state", {"chamber_temp": 30})
    assert bridge.send_device_command({"target_temp": 45}) is True
    cmd = [p for p in client.published if p[0] == "panda_breath/ABC123/command"]
    assert cmd and json.loads(cmd[0][1]) == {"target_temp": 45}


def test_send_device_command_without_id_is_noop():
    """Without a learned device id, outbound device command is declined."""
    bridge, client = make_bridge()
    bridge.start()
    client.fire_connect()
    assert bridge.send_device_command({"target_temp": 45}) is False


def test_publish_state_goes_to_plugin_topic():
    """Plugin state publications target the retained plugin state topic."""
    bridge, client = make_bridge()
    bridge.start()
    bridge.publish_state({"mode": "auto", "target_temp": 40})
    pub = [p for p in client.published if p[0] == "octoprint/pandabreath/state"]
    assert pub
    assert json.loads(pub[0][1])["mode"] == "auto"
    assert pub[0][3] is True  # retained


# ---- inbound command routing --------------------------------------------


def test_inbound_command_routes_to_handler():
    """Inbound plugin command messages are forwarded to command_handler."""
    seen = []
    bridge, client = make_bridge(
        command_handler=lambda action, data: seen.append((action, data))
    )
    bridge.start()
    client.fire_connect()
    client.fire_message(
        "octoprint/pandabreath/command",
        {"action": "set_target", "value": 45},
    )
    assert seen == [("set_target", {"action": "set_target", "value": 45})]


def test_inbound_command_ignored_when_control_disabled():
    """Inbound control commands are ignored when control path is disabled."""
    seen = []
    bridge, client = make_bridge(
        allow_control=False,
        command_handler=lambda a, d: seen.append((a, d)),
    )
    bridge.start()
    client.fire_connect()
    client.fire_message(
        "octoprint/pandabreath/command", {"action": "set_target", "value": 1}
    )
    assert not seen


def test_inbound_malformed_payload_is_swallowed():
    """Malformed command payloads are ignored without crashing."""
    seen = []
    bridge, client = make_bridge(command_handler=lambda a, d: seen.append((a, d)))
    bridge.start()
    client.fire_connect()
    client.fire_message("octoprint/pandabreath/command", b"not json")
    client.fire_message("octoprint/pandabreath/command", {"no": "action"})
    assert not seen


def test_inbound_handler_exception_does_not_propagate():
    """Exceptions in command handler are swallowed by bridge callback."""

    def boom(action, data):
        raise ValueError("bad value")

    bridge, client = make_bridge(command_handler=boom)
    bridge.start()
    client.fire_connect()
    # Must not raise — runs on the network thread.
    client.fire_message(
        "octoprint/pandabreath/command", {"action": "set_target", "value": 9}
    )


# ---- mode mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "ws,mqtt",
    [
        ("auto", "auto mode"),
        ("manual", "power on"),
        ("dry", "filament drying"),
    ],
)
def test_mode_mapping_roundtrip(ws, mqtt):
    """Mode mapping helpers convert both directions for known values."""
    assert MqttBridge.ws_mode_to_mqtt(ws) == mqtt
    assert MqttBridge.mqtt_mode_to_ws(mqtt) == ws


def test_mode_mapping_unknown_returns_none():
    """Unknown mode values map to None in both conversion directions."""
    assert MqttBridge.ws_mode_to_mqtt("standby") is None
    assert MqttBridge.mqtt_mode_to_ws("nonsense") is None


# ---- control sink: WS verb -> device MQTT command -----------------------


def _bridge_with_device():
    """Create and prime a bridge with a learned test device id."""
    bridge, client = make_bridge()
    bridge.start()
    client.fire_connect()
    client.fire_message("panda_breath/DEV/state", {"chamber_temp": 30})
    return bridge, client


def _last_device_cmd(client):
    """Return last native device command payload or None if none published."""
    cmds = [
        json.loads(p[1]) for p in client.published if p[0] == "panda_breath/DEV/command"
    ]
    return cmds[-1] if cmds else None


@pytest.mark.parametrize(
    "verb,params,expected",
    [
        ("set_target", {"value": 45}, {"target_temp": 45}),
        ("set_mode", {"mode": "auto"}, {"mode": "auto mode"}),
        ("set_mode", {"mode": "manual"}, {"mode": "power on"}),
        ("set_mode", {"mode": "dry"}, {"mode": "filament drying"}),
        ("set_filter_threshold", {"value": 40}, {"filter_temp": 40}),
        ("set_heater_threshold", {"value": 55}, {"heater_temp": 55}),
        ("set_dry_target", {"value": 50}, {"custom_temp": 50}),
        ("set_dry_timer", {"hours": 8}, {"custom_timer": 8}),
        ("heater_on", {}, {"work_on": "ON"}),
        ("heater_off", {}, {"work_on": "OFF"}),
        ("start_drying", {}, {"drying_running": "ON"}),
        ("stop_drying", {}, {"drying_running": "OFF"}),
        ("preset_pla", {}, {"filament_drying_mode": "pla"}),
        ("preset_petg", {}, {"filament_drying_mode": "petg"}),
    ],
)
def test_control_sink_maps_verb_to_device_command(verb, params, expected):
    """Supported control verbs are translated to expected device payloads."""
    bridge, client = _bridge_with_device()
    assert bridge.control_sink(verb, **params) is True
    assert _last_device_cmd(client) == expected


def test_control_sink_commit_dry_is_handled_noop():
    """commit_dry is acknowledged but emits no MQTT device command."""
    bridge, client = _bridge_with_device()
    # commit_dry has no MQTT command (number writes apply immediately) but
    # must report handled so the controller doesn't also send over WS.
    assert bridge.control_sink("commit_dry") is True
    assert _last_device_cmd(client) is None


def test_control_sink_declines_unmappable_verbs():
    """Unmappable control verbs are explicitly declined by control_sink."""
    bridge, _ = _bridge_with_device()
    assert bridge.control_sink("scan_printers") is False
    assert bridge.control_sink("refresh_settings") is False
    assert bridge.control_sink("emergency_stop") is False


def test_control_sink_declines_before_device_known():
    """control_sink declines while no device id is known yet."""
    bridge, client = make_bridge()
    bridge.start()
    client.fire_connect()
    # No device id yet → cannot address the command topic → decline.
    assert bridge.control_sink("set_target", value=45) is False


# ---- firmware gate ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("V1.0.4", True),
        ("1.0.4", True),
        ("V1.0.5", True),
        ("V1.1.0", True),
        ("V2.0.0", True),
        ("V1.0.10", True),  # the string-compare trap: 1.0.10 >= 1.0.4
        ("V1.0.3", False),
        ("1.0.0", False),
        ("V0.9.9", False),
        ("", False),
        (None, False),
        ("garbage", False),
    ],
)
def test_fw_supports_mqtt(raw, expected):
    """Firmware capability helper enforces minimum supported version."""
    from octoprint_pandabreath import fw_supports_mqtt

    assert fw_supports_mqtt(raw) is expected
