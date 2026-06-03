# MQTT

MQTT support is optional and intended for firmware versions that expose broker control.

## Typical setup

1. Enable MQTT in PandaBreath settings.
2. Enter broker host and port.
3. Keep allow-control disabled until connectivity is confirmed.
4. Enable control once status and topic flow look correct.

## Operational split

- MQTT: routine control and status topic traffic
- WebSocket: setup and safety-critical fallback paths
