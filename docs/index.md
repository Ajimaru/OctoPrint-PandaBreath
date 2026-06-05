# OctoPrint PandaBreath

OctoPrint PandaBreath provides direct control and status monitoring for the BIQU Panda Breath chamber heater.

## What you get

- Chamber target, mode and heater control from OctoPrint
- Live status in sidebar and tab views
- Safety controls including lock and emergency stop
- Optional MQTT bridge for day-to-day control paths

## Quick start

1. Install the plugin from the latest release ZIP.
2. Open plugin settings and enter the Panda Breath host.
3. Keep observe-only enabled for initial validation.
4. Verify status updates and frame flow.
5. Enable write control when validation is complete.

The full walkthrough is on the [Installation & Setup](installation.md) page.

!!! warning "Read the safety page first"
    This plugin controls a heater. Before enabling write control, read
    [Safety](safety.md) to understand observe-only mode, the safety lock,
    emergency stop, and the printer-link barrier.

## Documentation

- **[Installation & Setup](installation.md)** — install and validate safely
- **[Configuration](configuration.md)** — every setting, G-code, and the API
- **[Safety](safety.md)** — locks, emergency stop, and protective barriers
- **[MQTT](mqtt.md)** — optional broker-based control
- **[Troubleshooting](troubleshooting.md)** — diagnosing common problems
- **[Development](development.md)** — building the docs and contributing

## Links

- [Project README](https://github.com/Ajimaru/OctoPrint-PandaBreath#readme)
- [Releases](https://github.com/Ajimaru/OctoPrint-PandaBreath/releases)
- [Issues](https://github.com/Ajimaru/OctoPrint-PandaBreath/issues)
