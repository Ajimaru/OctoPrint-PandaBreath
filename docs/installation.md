# Installation & Setup

This page walks through installing PandaBreath and bringing it up safely for
the first time. The core principle: **validate in observe-only mode before
you ever let the plugin write to the device.**

## Requirements

- OctoPrint 1.10 or newer.
- A BIQU Panda Breath chamber heater reachable on your network.
- For MQTT control: device firmware **V1.0.4 or newer** (see [MQTT](mqtt.md)).

## Install

Install from the plugin's release archive:

1. In OctoPrint, open **Settings → Plugin Manager → Get More…**.
2. Choose **…from URL** and paste the latest release ZIP URL, or upload a
   downloaded ZIP under **…from an uploaded file**.
3. Restart OctoPrint when prompted.

Releases are published at
<https://github.com/Ajimaru/OctoPrint-PandaBreath/releases>.

## First-time validation workflow

Do not skip this. It confirms the plugin reads your specific device correctly
before any write command can reach the heater.

### 1. Enter the connection details

Open **Settings → PandaBreath** and set:

- **Transport**: `client` (unless you run a Bambu-emulation setup).
- **Client host**: the Panda Breath device IP or hostname.

Leave **observe-only enabled** (the default).

### 2. Confirm telemetry

Watch the sidebar and tab status. You should see:

- **Connection**: `online`.
- **Chamber** and **Target** temperatures updating.
- **Paired printer** showing a `printer_state` (binding → bound).

If you enabled the frame-history debug panel, confirm frames are flowing.

!!! tip "Use the logs"
    OctoPrint's log (and the optional persistent frame log) will show the
    bind sequence and any protocol errors. Resolve connection issues here,
    in observe-only, where nothing can be written.

### 3. Confirm the paired-printer link

The chamber will not heat unless the paired printer is **bound**
(`printer_state` 3). If the status shows *binding* or *unreachable*, fix the
pairing first — see the [printer-link barrier](safety.md#printer-link-barrier).

### 4. Enable write control

Only once telemetry is stable and the printer is bound:

1. Disable **observe-only**.
2. Start with a **conservative target** well under your `max_temp`.
3. Verify the heater responds and the chamber tracks the target.

### 5. (Optional) Enable automation and MQTT

With write control validated, you can enable:

- **G-code integration** (`M141`/`M191`) — see [Configuration](configuration.md#g-code-integration-m141-m191).
- **Auto on/off** around prints.
- The **MQTT bridge** — see [MQTT](mqtt.md).

## TLS setups

If your device or network requires `wss://`, enable **TLS** and provide the
CA/cert/key paths under [TLS settings](configuration.md#tls). Use
`tls_insecure` only for short-lived testing, never in production.

## Uninstall

Remove the plugin from **Settings → Plugin Manager** and restart OctoPrint.
The plugin stores no device state outside OctoPrint's own settings.

See also: [Configuration](configuration.md) · [Safety](safety.md) ·
[Troubleshooting](troubleshooting.md)
