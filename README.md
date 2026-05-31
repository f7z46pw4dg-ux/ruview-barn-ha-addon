# RuView Barn Bridge Add-on

Home Assistant add-on for the RuView ESP32 barn presence bridge.

The add-on listens for RuView UDP vitals packets on port `5005`, publishes
MQTT discovery/state for RuView barn sensors, and optionally calls Home
Assistant to turn the configured music switch on/off.

Default behavior:

- Presence on after 4 seconds above the configured score/motion thresholds
- Music switch on: `switch.music`
- Music switch off after 10 minutes of vacancy
- MQTT entities under `ruview/barn`
- Raw RuView firmware presence/motion flags are still published as attributes,
  but do not force occupancy unless `use_firmware_flags` is enabled.

Credentials are configured in Home Assistant add-on options, not in this
repository.
