# Third-party licenses

This plugin incorporates protocol layouts, field names and control-loop
ideas adapted from the following MIT-licensed upstream projects. Per the
MIT License, the original copyright notices and full license texts are
retained in this directory and must remain present in any redistribution.

## BIQU-Panda-Breath-Mod

- URL: <https://github.com/jeng37/BIQU-Panda-Breath-Mod>
- License: MIT
- Copyright: Copyright (c) [2026] [Jeng]
- License text: [BIQU-Panda-Breath-Mod-LICENSE](BIQU-Panda-Breath-Mod-LICENSE)
- Material adapted (from `Panda.py`):
  - Bind frame layout (`{"printer": {"ip", "sn", "access_code"}}`)
  - Settings/status field names (`warehouse_temper`, `set_temp`,
    `work_mode`, `work_on`, `fw_version`)
  - `work_mode` integer mapping: 1 = Auto, 2 = Manual, 3 = Dry, 0 = Standby

## chamber_control

- URL: <https://github.com/bula87/chamber_control>
- License: MIT
- Copyright: Copyright (c) 2026 Wojciech K
- License text: [chamber_control-LICENSE](chamber_control-LICENSE)
- Material adapted (from `chamber_control.py`):
  - `{"query": 1}` poll frame for read-only status pulls
  - `work_on` boolean-vs-int quirk handling
  - Reconnect/poll cadence

## Files containing derived material

Each of the files below carries an attribution header at the top of the
file with the same copyright notices reproduced verbatim from the
upstream LICENSE files:

- `octoprint_pandabreath/protocol.py` — frame layouts from both upstreams
- `octoprint_pandabreath/controller.py` — mode-name encoding tied to the
  Panda Breath firmware integer codes

## Scope of the adaptation

This plugin **does not** use MQTT or Home Assistant. The upstream
`Panda.py` additionally uses MQTT to bridge into Home Assistant; the
_WebSocket protocol toward the Panda Breath heater itself_ is plain JSON over
`ws://<panda>/ws`, and only that direct-protocol part has been adopted.
