# Configuration

All settings live under **Settings → PandaBreath** in OctoPrint. This page
documents every setting, its default, and what it does. Safety-related
settings are cross-referenced to the [Safety](safety.md) page.

## Connection

The plugin talks to the Panda Breath over a WebSocket. You only enter the
device address — the scheme (`ws://` vs `wss://`) and the `/ws` suffix are
derived from the TLS toggle automatically.

| Setting           | Default     | Description                                                                                                           |
| ----------------- | ----------- | ------------------------------------------------------------------------------------------------------------------- |
| `transport`       | `client`    | `client` connects to the device's own WebSocket; `server` is only for Bambu-emulation (BIQU-Panda-Breath-Mod) setups |
| `client_host`     | *(empty)*   | Host/IP of the Panda Breath device (client mode)                                                                    |
| `bind_host`       | `127.0.0.1` | Local bind address (server mode)                                                                                    |
| `bind_port`       | `8765`      | Local bind port (server mode)                                                                                       |
| `host_ip`         | *(empty)*   | Device IP used in bind frames                                                                                       |
| `serial_number`   | *(empty)*   | Device serial, for Bambu-emulation bind                                                                            |
| `access_code`     | *(empty)*   | Device access code, for Bambu-emulation bind                                                                       |
| `reconnect_delay` | `5.0`       | Seconds to wait before reconnecting after a drop                                                                   |

!!! note "Server mode is niche"
    Most users run **client mode**. Server mode exists for the
    Bambu-emulation style setups and needs the serial/access-code
    credentials so the device accepts the bind frame.

## TLS

Only relevant if your device or network policy requires `wss://`.

| Setting         | Default   | Description                                  |
| --------------- | --------- | -------------------------------------------- |
| `tls_enabled`   | `false`   | Use `wss://` for the WebSocket               |
| `tls_ca_file`   | *(empty)* | CA bundle path                               |
| `tls_cert_file` | *(empty)* | Client certificate path                      |
| `tls_key_file`  | *(empty)* | Client key path                              |
| `tls_insecure`  | `false`   | Skip certificate verification (testing only) |

## Safety settings

See [Safety](safety.md) for how these are enforced.

| Setting                | Default | Description                                                             |
| ---------------------- | ------- | ----------------------------------------------------------------------- |
| `observe_only`         | `true`  | Suppress all write frames; read-only. **Keep enabled until validated.** |
| `max_temp`             | `70.0`  | Hard chamber limit; caps targets and triggers the over-temp lock        |
| `timeout_seconds`      | `15.0`  | Watchdog stale-data threshold                                           |
| `navbar_estop_enabled` | `true`  | Show the emergency-stop button in the navbar                            |

## Automation

| Setting               | Default | Description                                             |
| --------------------- | ------- | ------------------------------------------------------- |
| `gcode_integration`   | `true`  | Honour `M141`/`M191` from the G-code stream (see below) |
| `auto_on_print_start` | `false` | Turn the chamber on when a print starts                 |
| `auto_off_print_end`  | `true`  | Cool down when a print ends                             |
| `print_start_target`  | `40.0`  | Target applied on print start (when auto-on is enabled) |

## Diagnostics

| Setting                    | Default | Description                                       |
| -------------------------- | ------- | ------------------------------------------------- |
| `debug_panel_enabled`      | `false` | Show the frame-history debug panel in the sidebar |
| `frame_log_enabled`        | `false` | Persist the WebSocket frame log to disk           |
| `frame_log_retention_days` | `7`     | Days of frame-log files to keep                   |

!!! tip "Frame log is for reverse-engineering"
    The in-memory ring buffer is enough for live inspection. Turn on the
    persistent frame log only when you need a longer capture to debug
    protocol behaviour — it adds disk I/O and noise.

## G-code integration (M141 / M191)

When `gcode_integration` is enabled, PandaBreath intercepts chamber
temperature commands from the G-code stream and re-targets the Panda Breath
chamber instead of forwarding them to the printer firmware (which usually
cannot handle them):

| G-code         | Meaning                          | Plugin behaviour                                                             |
| -------------- | -------------------------------- | --------------------------------------------------------------------------- |
| `M141 S<temp>` | Set chamber temperature          | Switches to **auto** mode, sets the target to `<temp>`                      |
| `M191 S<temp>` | Set chamber temperature and wait | Same re-targeting; the wait semantics are handled by the slicer/printer side |

Both commands are **swallowed** after handling, so they are not forwarded to
the printer. The `S` value is parsed as a float; the resulting target is
still subject to `max_temp` and the device limits.

!!! warning "Subject to safety gates"
    A G-code-driven target is applied through the same controller path as the
    UI. If the safety lock is engaged or the printer link is not bound, the
    re-target is refused exactly as a manual change would be.

## API commands

The plugin exposes a `SimpleApiPlugin` interface. Commands that mutate state
require the **Control** (or **Administer**) permission and are subject to the
safety lock and observe-only mode.

| Command                        | Parameters       | Notes                                                                                   |
| ------------------------------ | ---------------- | --------------------------------------------------------------------------------------- |
| `set_target`                   | `value`          | Chamber target (°C)                                                                      |
| `set_mode`                     | `mode`           | `auto` / `manual` / `dry` / `standby`                                                    |
| `set_heater`                   | `on`             | Power on/off; on is gated by the [printer-link barrier](safety.md#printer-link-barrier) |
| `set_custom_dry`               | `value`, `hours` | Dry target + timer in one transaction                                                    |
| `preset_pla` / `preset_petg`   | —                | Apply a built-in dry preset                                                              |
| `start_drying` / `stop_drying` | —                | Control the dry cycle                                                                    |
| `set_filter_threshold`         | `value`          | Filter-fan activation threshold                                                          |
| `set_heater_threshold`         | `value`          | Heater activation threshold                                                              |
| `scan_printers`                | —                | Trigger a printer scan                                                                   |
| `refresh_settings`             | —                | Re-read device settings                                                                  |
| `lock` / `unlock`              | —                | Engage/release the safety lock (Administer permission)                                   |
| `emergency_stop`               | —                | Hard stop (bypasses observe-only)                                                        |

Mutating commands return **409** under observe-only and **423** when locked.

See also: [Safety](safety.md) · [MQTT](mqtt.md) ·
[Troubleshooting](troubleshooting.md)
