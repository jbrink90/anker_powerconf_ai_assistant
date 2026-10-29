# Project TODO

## Voice interaction

- [x] Add quiet-room versus wake-phrase calibration and document threshold tuning.
- [x] Keep listening after a standalone wake phrase and remove the wake phrase when it precedes a request.
- [ ] Tune wake-word sensitivity on the Pi in normal rooms and check false triggers.
- [x] Restrict button discovery to devices advertising mapped keys, close unused device handles, and default to the named AVRCP device.
- [ ] Verify PowerConf button events and mappings on the actual device and firmware.
- [x] Add audio-route diagnostics, prefer likely PowerConf/Anker/BlueZ outputs, re-resolve output devices for each segment, and wait briefly for a Bluetooth sink to reconnect.
- [ ] Verify assistant speech and wake cues reach the PowerConf after startup and Bluetooth reconnects.
