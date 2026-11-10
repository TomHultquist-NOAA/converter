#!/usr/bin/env bash
# Independent AI/GEFS mean poller; schedule every 30 minutes.
# Uses the existing runtime, protected-cycle file and case/cron coordination.
set -euo pipefail
umask 022
export OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
BASE_DIR="${EVU_BASE_DIR:-/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing}"
mkdir -p "$BASE_DIR/logs"
exec >>"$BASE_DIR/logs/ai_incremental_$(date -u +%Y%m%d_%H%M%S)_$$.log" 2>&1
printf 'AI/GEFS poll START %s UTC PID=%s\n' "$(date -u +%FT%TZ)" "$$"
CONVERTER_ENV_FILE="${CONVERTER_ENV_FILE:-$BASE_DIR/converter_cron_env.sh}"
[[ -r "$CONVERTER_ENV_FILE" ]] || { echo "Missing converter runtime: $CONVERTER_ENV_FILE" >&2; exit 2; }
set +u; source "$CONVERTER_ENV_FILE"; set -u
CONVERTER_CONFIG_FILE="${CONVERTER_CONFIG_FILE:-$BASE_DIR/converter_config.sh}"
if [[ -r "$CONVERTER_CONFIG_FILE" ]]; then set +u; source "$CONVERTER_CONFIG_FILE"; set -u; fi
[[ "${AI_ENABLED:-1}" == 1 ]] || { echo 'AI poll disabled'; exit 0; }
PYTHON_BIN="${PYTHON_BIN:-python3}"
WGRIB2_BIN="${WGRIB2_BIN:-}"
[[ -x "$PYTHON_BIN" && -x "$WGRIB2_BIN" ]] || { echo 'Runtime Python or wgrib2 not executable' >&2; exit 127; }
source "$BASE_DIR/case_cron_coordination.sh"
if ! acquire_cron_case_gate "$BASE_DIR"; then echo 'AI poll skipped: historical-case priority'; exit 0; fi
exec 9>"$BASE_DIR/.ai_incremental.lock"
if ! flock -n 9; then echo 'AI poll skipped: earlier AI poll still running'; exit 0; fi
AI_MODELS="${AI_MODELS:-all}"
AI_LAG_HOURS="${AI_LAG_HOURS:-4.0}"
AI_CATCHUP_CYCLES="${AI_CATCHUP_CYCLES:-3}"
# Local operational GRIB reads can run much faster than external downloads.
# AI_WORKERS remains a legacy fallback for AWS only; use independent knobs.
AI_LOCAL_WORKERS="${AI_LOCAL_WORKERS:-8}"
AI_REMOTE_WORKERS="${AI_REMOTE_WORKERS:-${AI_WORKERS:-3}}"
AI_SOURCE_MODE="${AI_SOURCE_MODE:-local}"
AI_NOMADS_FALLBACK="${AI_NOMADS_FALLBACK:-0}"
AI_MAX_HOUR_JOBS="${AI_MAX_HOUR_JOBS:-12}"
AI_RETRY_MINUTES="${AI_RETRY_MINUTES:-45}"
RETENTION_DAYS="${AI_RETENTION_DAYS:-${RETENTION_DAYS:-7}}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$BASE_DIR/zarr}"
WORK_ROOT="${WORK_ROOT:-$BASE_DIR/work}"
PUBLISH_HOST="${PUBLISH_HOST:-thultquist@rzdm.ncep.noaa.gov}"
PUBLISH_DIR="${PUBLISH_DIR:-/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data}"
PROTECTED_CYCLES_FILE="${PROTECTED_CYCLES_FILE:-$BASE_DIR/protected_cycles.json}"
NO_PUBLISH="${NO_PUBLISH:-0}"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( RETENTION_DAYS >= 1 && RETENTION_DAYS <= 366 )) || { echo 'Invalid AI retention'; exit 2; }
# Low-overhead whole-poll samples for the AI model family. Each ai_to_zarr
# child also records per-model/hour task timings when telemetry is enabled.
# Start AFTER both case and incremental locks to avoid reports for skipped polls.
EVU_PERF_ENABLED="${EVU_PERF_ENABLED:-0}"
EVU_PERF_SAMPLE_SECONDS="${EVU_PERF_SAMPLE_SECONDS:-5}"
EVU_PERF_RETENTION_DAYS="${EVU_PERF_RETENTION_DAYS:-$RETENTION_DAYS}"
if [[ "$EVU_PERF_RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( EVU_PERF_RETENTION_DAYS > RETENTION_DAYS )); then
  EVU_PERF_RETENTION_DAYS="$RETENTION_DAYS"
fi
EVU_PERF_LOG_DIR="${EVU_PERF_LOG_DIR:-$BASE_DIR/logs/performance}"
export EVU_PERF_ENABLED EVU_PERF_SAMPLE_SECONDS EVU_PERF_RETENTION_DAYS EVU_PERF_LOG_DIR
AI_PERF_MONITOR_PID=""
ai_perf_cleanup() {
  if [[ -n "${AI_PERF_MONITOR_PID:-}" ]]; then
    kill -TERM "$AI_PERF_MONITOR_PID" 2>/dev/null || true
    wait "$AI_PERF_MONITOR_PID" 2>/dev/null || true
  fi
}
trap ai_perf_cleanup EXIT
if [[ "$EVU_PERF_ENABLED" == 1 && -r "$BASE_DIR/perf_monitor.py" ]]; then
  if mkdir -p "$EVU_PERF_LOG_DIR" 2>/dev/null; then
    "$PYTHON_BIN" "$BASE_DIR/perf_monitor.py" --watch-pid "$$" \
      --label "ai_incremental_cron" --log-dir "$EVU_PERF_LOG_DIR" \
      --interval "$EVU_PERF_SAMPLE_SECONDS" --retention-days "$EVU_PERF_RETENTION_DAYS" &
    AI_PERF_MONITOR_PID=$!
  else
    echo "WARNING: AI performance log directory unavailable: $EVU_PERF_LOG_DIR" >&2
  fi
fi

# Optional provider endpoint overrides are environment settings, not hardcoded
# alternate mirrors; named exactly as in ai_to_zarr.py's --*-base arguments.
[[ "$AI_LOCAL_WORKERS" =~ ^[0-9]+$ ]] && (( AI_LOCAL_WORKERS >= 1 )) || { echo 'Invalid AI_LOCAL_WORKERS'; exit 2; }
[[ "$AI_REMOTE_WORKERS" =~ ^[0-9]+$ ]] && (( AI_REMOTE_WORKERS >= 1 )) || { echo 'Invalid AI_REMOTE_WORKERS'; exit 2; }
[[ "$AI_SOURCE_MODE" == local || "$AI_SOURCE_MODE" == remote ]] || { echo 'AI_SOURCE_MODE must be local or remote'; exit 2; }
[[ "$AI_NOMADS_FALLBACK" == 0 || "$AI_NOMADS_FALLBACK" == 1 ]] || { echo 'Invalid AI_NOMADS_FALLBACK'; exit 2; }
extra=(--local-workers "$AI_LOCAL_WORKERS" --remote-workers "$AI_REMOTE_WORKERS" --ai-source "$AI_SOURCE_MODE")
[[ "$AI_NOMADS_FALLBACK" == 0 ]] || extra+=(--nomads-fallback)
[[ "$NO_PUBLISH" != 1 ]] || extra+=(--no-publish)
[[ -z "${AI_AIGFS_BASE:-}" ]] || extra+=(--aigfs-base "$AI_AIGFS_BASE")
[[ -z "${AI_AIGEFS_BASE:-}" ]] || extra+=(--aigefs-mean-base "$AI_AIGEFS_BASE")
[[ -z "${AI_HGEFS_BASE:-}" ]] || extra+=(--hgefs-mean-base "$AI_HGEFS_BASE")
[[ -z "${AI_GEFS_BASE:-}" ]] || extra+=(--gefs-mean-base "$AI_GEFS_BASE")
[[ -z "${AI_AIGFS_LOCAL_BASE:-}" ]] || extra+=(--aigfs-local-base "$AI_AIGFS_LOCAL_BASE")
[[ -z "${AI_AIGEFS_LOCAL_BASE:-}" ]] || extra+=(--aigefs-mean-local-base "$AI_AIGEFS_LOCAL_BASE")
[[ -z "${AI_HGEFS_LOCAL_BASE:-}" ]] || extra+=(--hgefs-mean-local-base "$AI_HGEFS_LOCAL_BASE")
echo "AI acquisition: $AI_SOURCE_MODE; local workers=$AI_LOCAL_WORKERS; remote workers=$AI_REMOTE_WORKERS; NOMADS fallback=$AI_NOMADS_FALLBACK"
# A lag is an initial *eligibility* threshold, not an assumption that all
# forecast leads are present. Each poll retries unavailable leads.
mapfile -t CYCLES < <("$PYTHON_BIN" - "$AI_LAG_HOURS" "$AI_CATCHUP_CYCLES" <<'PY'
import datetime as d,sys
lag=float(sys.argv[1]);back=int(sys.argv[2])
if not 0<=lag<=12 or not 0<=back<=20:raise SystemExit('AI lag must be 0..12 hours, catchup 0..20 cycles')
now=d.datetime.now(d.timezone.utc)-d.timedelta(hours=lag)
start=now.replace(hour=(now.hour//6)*6,minute=0,second=0,microsecond=0)
for k in range(back+1):print((start-d.timedelta(hours=6*k)).strftime('%Y%m%d %H'))
PY
)
status=0
for item in "${CYCLES[@]}"; do
  read -r date cycle <<<"$item"
  echo "AI/GEFS poll $date $cycle UTC"
  if ! "$PYTHON_BIN" "$BASE_DIR/ai_to_zarr.py" \
      --date "$date" --cycle "$cycle" --models "$AI_MODELS" \
      --max-hour-jobs "$AI_MAX_HOUR_JOBS" \
      --retry-failed-minutes "$AI_RETRY_MINUTES" --retention-days "$RETENTION_DAYS" \
      --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      --wgrib2 "$WGRIB2_BIN" "${extra[@]}"; then
    echo "WARNING AI/GEFS $date $cycle failed; later polls will retry"
    status=1
  fi
done
if [[ -f "$BASE_DIR/cleanup_cron_logs.py" ]]; then
  "$PYTHON_BIN" "$BASE_DIR/cleanup_cron_logs.py" --logs-dir "$BASE_DIR/logs" \
    --retention-days "$RETENTION_DAYS" --prefix ai_incremental || true
fi
printf 'AI/GEFS poll END %s UTC status=%s\n' "$(date -u +%FT%TZ)" "$status"
exit "$status"
