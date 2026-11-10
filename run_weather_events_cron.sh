#!/usr/bin/env bash
# Optional independent read-only scanner and atomic publisher. Requires Python >=3.6.
set -euo pipefail
cd "$(dirname "$0")"
CONFIG="${WEATHER_EVENT_CONFIG:-$(pwd)/weather_events_cron_config.sh}"
if [[ ! -f "$CONFIG" ]]; then
  echo "Weather event cron config missing: $CONFIG" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"
: "${WEATHER_EVENT_DATA_ROOT:?Set WEATHER_EVENT_DATA_ROOT to the local Zarr catalog directory}"
: "${WEATHER_EVENT_WORK_DIR:?Set WEATHER_EVENT_WORK_DIR}"
command=("${WEATHER_EVENT_PYTHON:-python3}" weather_event_publish.py --data-root "$WEATHER_EVENT_DATA_ROOT" --work-dir "$WEATHER_EVENT_WORK_DIR" --recent-cycles "${WEATHER_EVENT_RECENT_CYCLES:-1}" --max-hour "${WEATHER_EVENT_MAX_HOUR:-72}" --hour-step "${WEATHER_EVENT_HOUR_STEP:-6}" --max-frames-per-model "${WEATHER_EVENT_MAX_FRAMES_PER_MODEL:-12}" --max-events "${WEATHER_EVENT_MAX_EVENTS:-15}" --max-cycle-age-hours "${WEATHER_EVENT_MAX_CYCLE_AGE_HOURS:-36}" --publish-target "${WEATHER_EVENT_PUBLISH_TARGET:-}")
if [[ "${WEATHER_EVENT_SKIP_ALERTS:-0}" == 1 ]]; then command+=(--skip-alerts); fi
if [[ "${WEATHER_EVENT_STRICT_ALERTS:-0}" == 1 ]]; then command+=(--strict-alerts); fi
if command -v nice >/dev/null 2>&1; then exec nice -n 10 "${command[@]}"; fi
exec "${command[@]}"
