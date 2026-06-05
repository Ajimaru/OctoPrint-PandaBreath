# Troubleshooting

Most problems show up in two places: the **live status** in the OctoPrint UI
and the **OctoPrint log** (`octoprint.log`). The plugin logs everything under
the `PandaBreath:` prefix, so filtering the log for that string is the fastest
way to diagnose an issue.

!!! tip "Validate in observe-only"
    If you are debugging connection or telemetry problems, keep
    [observe-only](safety.md#observe-only-mode) enabled. It removes any chance
    that the plugin writes to the device while you investigate.

## Connection problems

### Status shows "offline" / no telemetry

- **`client_host not set — adapter idle`** in the log: you have not entered
  the device host. Set **Client host** in [Configuration](configuration.md#connection).
- Wrong IP/hostname, or the device is on a different subnet: verify you can
  reach the device from the OctoPrint host (e.g. `ping`, or open the device's
  own web UI).
- TLS mismatch: if the device needs `wss://`, enable
  [TLS](configuration.md#tls). If it does not, make sure TLS is **off**.

### Connection drops and reconnects repeatedly

- Network instability between OctoPrint and the device.
- `reconnect_delay` controls the backoff; the adapter will keep retrying.
- Check the persistent [frame log](configuration.md#diagnostics) for the
  point of failure.

## Printer-link problems

### Status shows "binding" and never reaches "bound"

The Panda Breath is trying to pair with its printer but cannot complete the
link. While in this state the chamber **cannot heat** — this is by design (the
[printer-link barrier](safety.md#printer-link-barrier)).

- Confirm the paired printer is powered on and reachable on the network.
- For Bambu-emulation/server-mode setups, verify the serial number and access
  code are correct so the bind frame is accepted.

### Status shows "unreachable"

The paired printer cannot be reached. Heating is blocked and any running
heater is forced off. Restore the printer's network connection; the
`printer_link` lock auto-releases once the device reports *bound* again.

### Power button is disabled with a warning

This is expected when the printer link is binding or unreachable. The warning
explains it: power is disabled until the paired printer is connected. See the
[printer-link barrier](safety.md#printer-link-barrier).

## Heater shuts off unexpectedly

Check `last_safety_reason` (shown in the status / diagnostics):

| Log / reason                                 | Cause                                     | Recovery                                                   |
| -------------------------------------------- | ----------------------------------------- | ---------------------------------------------------------- |
| `no data for … — locking` (`timeout`)        | Watchdog: no frames for `timeout_seconds` | Restore the connection; a fresh frame auto-clears the lock |
| `chamber … exceeds hard limit` (`over_temp`) | Chamber exceeded `max_temp`               | Investigate the heat source, then **Unlock**               |
| `printer link not bound` (`printer_link`)    | Printer binding/unreachable               | Restore the printer link; auto-clears when bound           |
| `EMERGENCY STOP triggered` (`estop`)         | Emergency stop pressed                    | **Unlock** when ready                                       |

See [Safety → Recovery checklist](safety.md#recovery-checklist) for the full
table.

## MQTT problems

### MQTT does not start

- **`mqtt_enabled but no broker host set`**: enter the broker host in
  [MQTT settings](mqtt.md).
- **`mqtt_enabled but paho-mqtt not installed`**: the `paho-mqtt` dependency
  is missing; reinstall the plugin so its dependencies are pulled in.
- **`MQTT requires firmware V1.0.4+`**: your device firmware predates the
  MQTT control interface. Update the firmware or use the WebSocket transport.
- **`mqtt_enabled and broker configured, waiting`**: the bridge is waiting for
  the device to report its firmware version before starting. This clears once
  a status frame arrives.

### Commands ignored over MQTT

- **`MQTT dry command ignored — printer busy`**: dry-mode commands are
  refused while the paired printer is running a job — the chamber is needed
  for printing, not drying.
- **`refused emergency_stop over MQTT`**: emergency stop is intentionally not
  accepted over MQTT; use the WebSocket/UI path for safety-critical stops.
- **`unknown MQTT command`**: the published command name is not recognised;
  check your topic/payload against [MQTT](mqtt.md).

## Firmware version checks

- **`could not fetch latest firmware info`** / **`could not parse latest
  firmware from docs`**: the plugin could not reach or parse the upstream
  firmware docs to determine the latest version. This is non-fatal — it only
  affects the "update available" hint, not control.

## Frame logging for deep debugging

When you need to understand exactly what the device sent, enable the
persistent [frame log](configuration.md#diagnostics):

- Set `frame_log_enabled` to capture frames to disk.
- Files are retained for `frame_log_retention_days` (default 7).
- Combine with the sidebar **debug panel** for a live ring-buffer view.

If you suspect a frame-log write problem, look for **`frame log write
failed`** in the log.

## Filing an issue

If you cannot resolve a problem, open an issue at
<https://github.com/Ajimaru/OctoPrint-PandaBreath/issues> and include:

- The relevant `PandaBreath:` lines from `octoprint.log`.
- Your transport mode, firmware version, and whether observe-only was on.
- The live status values (connection, printer_state, last_safety_reason).

See also: [Safety](safety.md) · [Configuration](configuration.md) ·
[MQTT](mqtt.md)
