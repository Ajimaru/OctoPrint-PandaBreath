# Configuration

## Connection

Set transport mode and Panda Breath host in plugin settings.

- Client mode: connect to the device WebSocket endpoint
- Server mode: only for Bambu-emulation setups

## Safety defaults

- Observe-only is enabled by default
- Max temperature limits target values
- Timeout watchdog blocks unsafe stale control

## Control enablement

After confirming stable telemetry, disable observe-only and start with conservative targets.
