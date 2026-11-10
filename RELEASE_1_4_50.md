# OMD EVU Converter v1.4.50 — 2026-11-10

- Default `max-events` lowered from 25 to **15** in the scanner, optional scheduled publisher, cron wrapper fallback, and example cron configuration. An existing `weather_events_cron_config.sh` must be edited manually; no operational converter settings are affected.
- Added a **soft headline overlap penalty** for selected situations with the same primary weather category, within 450 km and overlapping or within 12 hours. Repetitive headlines receive less priority; stronger cases may still be chosen. This is ranking only, not weather feature tracking or a new exclusion mask.
- No changes to native measurements, meteorological thresholds, full candidate detections, WWA matching, published JSON schema, data retention, or model conversion.
- Full candidate-group summaries remain available for audit; selected events remain capped by `WEATHER_EVENT_MAX_EVENTS`.
