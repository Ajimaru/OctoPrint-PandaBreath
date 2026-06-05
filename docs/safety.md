# Safety

The chamber heater controlled by this plugin can reach temperatures that
soften or ignite materials and can burn skin. PandaBreath layers several
independent safety mechanisms so that a single failure — a stale connection,
an unreachable printer, an over-temperature reading — does not leave the
chamber heating unattended. This page documents each mechanism, how it
behaves, and how to recover from it.

!!! warning "The plugin is an assistant, not a replacement for the device"
    The Panda Breath firmware enforces its own limits. PandaBreath mirrors
    those limits and adds OctoPrint-side guards, but you remain responsible
    for safe operation, adequate ventilation, and not leaving prints
    unattended.

## Observe-only mode

Observe-only is the **default** and the single most important safety setting.

- The adapter connects, binds, and polls the device, but **suppresses every
  write frame**.
- The controller stops issuing its own heater on/off commands.
- The HTTP API rejects mutating commands with **HTTP 409 (Conflict)**.

Keep observe-only enabled until you have confirmed, from the logs and the
live status, that the plugin reads your device correctly. Only then disable it
to allow write control.

!!! note "Emergency stop still works in observe-only"
    The emergency-stop `heater_off` frame is on the observe-safe whitelist, so
    it always reaches the device even while normal writes are suppressed.

## Safety lock

The safety lock is a single internal flag that, when engaged, **forces the
heater off and blocks power-on**. It can be triggered for five distinct
reasons. Turning the heater **off** is always allowed regardless of the lock.

| Reason (`last_safety_reason`) | Trigger                                    | Releases                              |
| ----------------------------- | ------------------------------------------ | ------------------------------------- |
| `user`                        | Operator pressed Lock                      | Manually, via Unlock                  |
| `estop`                       | Emergency stop                             | Manually, via Unlock                  |
| `over_temp`                   | Chamber exceeded the configured `max_temp` | Manually, via Unlock                  |
| `timeout`                     | Watchdog saw no data for `timeout_seconds` | Auto, on the next fresh status frame  |
| `printer_link`                | Paired printer is binding or unreachable   | Auto, once the printer is bound again |

The two **auto-releasing** locks (`timeout`, `printer_link`) clear themselves
when the underlying condition resolves. The three **manual** locks (`user`,
`estop`, `over_temp`) stay engaged until you press Unlock — they represent a
deliberate or hard-fault state that an operator should acknowledge.

!!! important "Locks do not override each other"
    An auto-releasing lock never clobbers a manual one. If you engage a `user`
    lock and the printer link then drops and recovers, the `user` lock
    survives — only the operator can release it.

## Emergency stop

Emergency stop is a hard stop that shuts the heater down **regardless of
observe-only**. It sends a single `heater_off` frame — the same mechanism the
Panda Breath's own WebUI "Work Mode off" toggle uses, which is the only stop
the device firmware exposes. The firmware then stops any running dry cycle and
turns the heater off internally.

- Exposed as a navbar button (toggle with the `navbar_estop_enabled` setting).
- Engages a manual-class lock (`estop`) that must be released manually.
- Never surfaces transport errors: the internal state flips to locked even if
  the wire frame fails to send, so the safety guarantee holds.

## Printer-link barrier

The chamber must not heat unless the paired printer is reachable. The device
reports a `printer_state` in every status frame:

| `printer_state` | Meaning                                 | Heating     |
| --------------- | --------------------------------------- | ----------- |
| `2`             | Binding — link still being established  | **Blocked** |
| `3`             | Bound — printer reachable               | Allowed     |
| `4`             | Unreachable — printer cannot be reached | **Blocked** |
| *(absent)*      | Older firmware that omits the field     | Not gated   |

While the link is binding (2) or unreachable (4):

- `set_heater(on=True)` is refused with **HTTP 423 (Locked)**.
- If the heater was already running, the status loop engages a `printer_link`
  lock and **forces a `heater_off` frame** to the device.
- The power-on control in the UI is disabled, with an explanatory warning.

When the printer becomes bound (3) again, the `printer_link` lock
auto-releases and power-on is permitted.

## Over-temperature cutoff

Every status frame is checked against the configurable `max_temp` (default
70 °C). If the chamber reading exceeds it, the controller engages an
`over_temp` lock and shuts the heater off. This is a **manual** lock: it
requires an operator Unlock, because exceeding the hard limit indicates the
chamber ran hotter than intended and warrants attention.

`max_temp` also caps the upper bound of any target temperature you set, so the
plugin refuses to *request* a target above the limit in the first place.

## Watchdog (stale-data timeout)

A background watchdog tracks the time since the last received frame. If no
data arrives for `timeout_seconds` (default 15 s), it engages a `timeout`
lock and shuts the heater off — heating blind, with no telemetry, is unsafe.

This lock **auto-releases** on the next fresh status frame, because renewed
data flow means the condition has resolved on its own.

## Permissions

PandaBreath registers three OctoPrint permissions so administrators can scope
who may do what:

| Permission                  | Allows                                  |
| --------------------------- | --------------------------------------- |
| **View chamber status**     | Reading the chamber state (read-only)   |
| **Control chamber heater**  | Changing target, mode, and heater state |
| **Administer chamber lock** | Locking/unlocking the safety interlock  |

Grant control and admin permissions only to trusted operators.

## Device input limits

The plugin mirrors the firmware's reverse-engineered limits and clamps or
rejects out-of-range input before sending a frame:

| Setting               | Min | Max                               |
| --------------------- | --- | --------------------------------- |
| Chamber target (°C)   | 0   | 60 (further capped by `max_temp`) |
| Filter threshold (°C) | 0   | 120                               |
| Heater threshold (°C) | 40  | 120                               |
| Dry target (°C)       | 40  | 60                                |
| Dry timer (hours)     | 1   | 99                                |

## Recovery checklist

| Symptom                                  | Likely lock      | Action                                                           |
| ---------------------------------------- | ---------------- | ---------------------------------------------------------------- |
| Power-on refused, "printer link" warning | `printer_link`   | Wait for the printer to become reachable; the lock clears itself |
| Heater shut off, no recent status        | `timeout`        | Check the device connection; a fresh frame auto-clears it        |
| Heater shut off after a hot reading      | `over_temp`      | Investigate the cause, then press Unlock                         |
| Locked after pressing Stop               | `estop` / `user` | Press Unlock when ready to resume                                |

See also: [Configuration](configuration.md) ·
[Troubleshooting](troubleshooting.md)
