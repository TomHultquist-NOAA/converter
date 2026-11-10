# COPY to weather_events_cron_config.sh and adjust paths before enabling cron.
# This file is sourced by a trusted local shell script: do not edit with untrusted input.
WEATHER_EVENT_PYTHON=python3
WEATHER_EVENT_DATA_ROOT=/path/to/actual/zarr/data
WEATHER_EVENT_WORK_DIR=/path/to/weather_events_work
# Leave EMPTY for preview-only runs. To publish the validated feed, use exactly ONE:
# WEATHER_EVENT_PUBLISH_TARGET='local:/absolute/path/to/viewer/data/weather_events.json'
# WEATHER_EVENT_PUBLISH_TARGET='ssh:user@hostname:/absolute/path/to/viewer/data/weather_events.json'
WEATHER_EVENT_PUBLISH_TARGET=''
# Forecast screening depth: F006..F072 at six-hour intervals (when data are available).
# This is more comprehensive than the original F012/F024/F036/F048 test scan.
# The frame cap should be >= the number of desired sampled hours.
WEATHER_EVENT_RECENT_CYCLES=1
WEATHER_EVENT_MAX_HOUR=72
WEATHER_EVENT_HOUR_STEP=6
WEATHER_EVENT_MAX_FRAMES_PER_MODEL=12
WEATHER_EVENT_MAX_EVENTS=15
WEATHER_EVENT_MAX_CYCLE_AGE_HOURS=36
# 0=collect (IEM historical/active alert context); 1=omit alert context (offline mode)
WEATHER_EVENT_SKIP_ALERTS=0
# 0=publish even with unavailable alerts (show status); 1=require every lookup to succeed
WEATHER_EVENT_STRICT_ALERTS=0
