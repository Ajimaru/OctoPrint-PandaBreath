# MQTT

The MQTT bridge is optional and requires device firmware **V1.0.4 or newer**
— earlier firmware does not expose the broker control interface. The plugin
refuses to start the bridge until the device has reported a supported
firmware version over the WebSocket.

## Why MQTT

The Panda Breath's WebSocket does not acknowledge write frames, which forces
a reconnect after every change. MQTT acknowledges delivery and streams state,
so day-to-day control avoids that reconnect churn.

The split of responsibilities:

- **MQTT**: routine control (target, mode, heater, drying, thresholds) and
  status telemetry.
- **WebSocket**: setup, emergency stop, watchdog and every safety-critical
  path. Safety frames are **never** routed over MQTT — a broker outage can
  never block a heater-off.

Emergency stop is refused over MQTT by design; use the UI or HTTP API.

## Prerequisites

1. Device firmware V1.0.4+.
2. The Panda Breath itself bound to your broker via its own **Bind a
   Broker** WebUI menu — the device then publishes to
   `panda_breath/<id>/state` and listens on `panda_breath/<id>/command`.
3. The `paho-mqtt` (v2) Python package — installed automatically with the
   plugin.

## Settings

| Setting                    | Default                 | Description                                                                                      |
| -------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------ |
| `mqtt_enabled`             | `false`                 | Master switch for the bridge                                                                     |
| `mqtt_host`                | *(empty)*               | Broker host; the bridge stays inactive while empty                                               |
| `mqtt_port`                | `1883`                  | Broker port                                                                                      |
| `mqtt_username`            | *(empty)*               | Broker username (optional; admin-only setting)                                                   |
| `mqtt_password`            | *(empty)*               | Broker password (optional; admin-only setting)                                                   |
| `mqtt_base_topic`          | `octoprint/pandabreath` | Plugin-owned topic namespace for state + command                                                 |
| `mqtt_use_appearance_name` | `true`                  | Append OctoPrint's appearance name to the base topic so multiple instances share a broker safely |
| `mqtt_allow_control`       | `true`                  | Accept inbound commands on `<base>/command`; disable for a publish/telemetry-only bridge         |

`mqtt_allow_control` is **enabled by default**. If you want to validate
connectivity with zero write exposure first, disable it, confirm the state
topic flows, then re-enable it. Inbound commands always run through the same
validation as the HTTP API — device ranges, safety lock and observe-only all
apply.

## Topics

The bridge uses two topic families:

| Topic                       | Direction       | Purpose                                                              |
| --------------------------- | --------------- | -------------------------------------------------------------------- |
| `panda_breath/<id>/state`   | device → plugin | Native device state (~2 Hz); also used to discover the device `<id>` |
| `panda_breath/<id>/command` | plugin → device | Native device control commands                                       |
| `<base>/state`              | plugin → broker | The plugin's own controller snapshot (retained)                      |
| `<base>/command`            | broker → plugin | Inbound control commands (when `mqtt_allow_control` is on)           |

`<base>` is `mqtt_base_topic`, plus `/<appearance-name>` (URL-encoded) when
`mqtt_use_appearance_name` is enabled and OctoPrint has an appearance name
set.

Inbound commands on `<base>/command` use the controller verb form:

```json
{ "action": "set_target", "value": 45 }
{ "action": "set_mode", "mode": "auto" }
```

## Typical setup

1. Bind the Panda Breath itself to your broker (device WebUI).
2. Enable MQTT in PandaBreath settings and enter broker host/port
   (credentials if your broker needs them).
3. Save. The bridge starts once the device has reported firmware V1.0.4+.
4. Confirm the `<base>/state` topic updates and the device id was
   discovered (see the OctoPrint log, `MqttBridge:` prefix).
5. Drive the chamber from the UI — control now flows over MQTT, with the
   WebSocket as automatic fallback.

See also: [Configuration](configuration.md) · [Safety](safety.md) ·
[Troubleshooting](troubleshooting.md#mqtt-problems)
