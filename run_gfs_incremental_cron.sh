#!/usr/bin/env bash
# Optional independent GFS v16/v17 poller; run every 30 minutes.
# Runs only forecast conversion; the established verification pipeline owns analyses.
set -euo pipefail
umask 022
export OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
BASE_DIR="${EVU_BASE_DIR:-/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing}"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/gfs_incremental_$(date -u +%Y%m%d_%H%M%S)_$$.log" 2>&1
printf 'GFS POLL START %s UTC pid=%s\n' "$(date -u +%FT%TZ)" "$$"
CONVERTER_ENV_FILE="${CONVERTER_ENV_FILE:-$BASE_DIR/converter_cron_env.sh}"
if [[ ! -r "$CONVERTER_ENV_FILE" ]]; then echo "Missing $CONVERTER_ENV_FILE" >&2; exit 2; fi
set +u; source "$CONVERTER_ENV_FILE"; set -u
CONVERTER_CONFIG_FILE="${CONVERTER_CONFIG_FILE:-$BASE_DIR/converter_config.sh}"
if [[ -r "$CONVERTER_CONFIG_FILE" ]]; then
  set +u; source "$CONVERTER_CONFIG_FILE"; set -u
fi
[[ "${GFS_ENABLED:-1}" == 1 ]] || { echo 'GFS workflow disabled'; exit 0; }
PYTHON_BIN="${PYTHON_BIN:-python3}"
WGRIB2_BIN="${WGRIB2_BIN:-}"
if [[ ! -x "$PYTHON_BIN" || ! -x "$WGRIB2_BIN" ]]; then
  echo 'Capture the executable paths in the shared runtime environment before running GFS' >&2; exit 127
fi
source "$BASE_DIR/case_cron_coordination.sh"
if ! acquire_cron_case_gate "$BASE_DIR"; then
  echo 'GFS poll skipped: historical-case priority'; exit 0
fi
exec 9>"$BASE_DIR/.gfs_incremental.lock"
if ! flock -n 9; then echo 'GFS poll skipped: prior poll still active'; exit 0; fi
GFS_MODELS="${GFS_MODELS:-all}"
GFS_LAG_HOURS="${GFS_LAG_HOURS:-3.5}"
GFS_CATCHUP_CYCLES="${GFS_CATCHUP_CYCLES:-8}"
GFS_WORKERS="${GFS_WORKERS:-4}"
GFS_MAX_HOUR_JOBS="${GFS_MAX_HOUR_JOBS:-0}"
GFS_RETRY_MINUTES="${GFS_RETRY_MINUTES:-45}"
RETENTION_DAYS="${GFS_RETENTION_DAYS:-${RETENTION_DAYS:-7}}"
if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || (( RETENTION_DAYS < 1 || RETENTION_DAYS > 366 )); then echo "Invalid GFS retention" >&2; exit 2; fi
OUTPUT_ROOT="${OUTPUT_ROOT:-$BASE_DIR/zarr}"
WORK_ROOT="${WORK_ROOT:-$BASE_DIR/work}"
PUBLISH_HOST="${PUBLISH_HOST:-thultquist@rzdm.ncep.noaa.gov}"
PUBLISH_DIR="${PUBLISH_DIR:-/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data}"
PROTECTED_CYCLES_FILE="${PROTECTED_CYCLES_FILE:-$BASE_DIR/protected_cycles.json}"
GFS_V17_BASE_DIR="${GFS_V17_BASE_DIR:-/lfs/h2/emc/gfstemp/emc.global/comroot/retrov17_01_realtime}"
NO_PUBLISH="${NO_PUBLISH:-0}"
GFS_SKIP_PROFILES="${GFS_SKIP_PROFILES:-0}"
GFS_MARINE_ENABLED="${GFS_MARINE_ENABLED:-1}"
# Whole-cron performance monitoring (same reporting directory and limits as
# the other incremental wrapper). The sampler observes the GFS process tree;
# it never limits workers or changes forecast selection.
EVU_PERF_ENABLED="${EVU_PERF_ENABLED:-0}"
EVU_PERF_SAMPLE_SECONDS="${EVU_PERF_SAMPLE_SECONDS:-5}"
EVU_PERF_RETENTION_DAYS="${EVU_PERF_RETENTION_DAYS:-$RETENTION_DAYS}"
if [[ "$EVU_PERF_RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( EVU_PERF_RETENTION_DAYS > RETENTION_DAYS )); then
  EVU_PERF_RETENTION_DAYS="$RETENTION_DAYS"
fi
EVU_PERF_LOG_DIR="${EVU_PERF_LOG_DIR:-$BASE_DIR/logs/performance}"
export EVU_PERF_ENABLED EVU_PERF_SAMPLE_SECONDS EVU_PERF_RETENTION_DAYS EVU_PERF_LOG_DIR
GFS_PERF_MONITOR_PID=""
gfs_perf_cleanup() {
  if [[ -n "${GFS_PERF_MONITOR_PID:-}" ]]; then
    kill -TERM "$GFS_PERF_MONITOR_PID" 2>/dev/null || true
    wait "$GFS_PERF_MONITOR_PID" 2>/dev/null || true
  fi
}
trap gfs_perf_cleanup EXIT
if [[ "$EVU_PERF_ENABLED" == 1 && -r "$BASE_DIR/perf_monitor.py" ]]; then
  if mkdir -p "$EVU_PERF_LOG_DIR" 2>/dev/null; then
    "$PYTHON_BIN" "$BASE_DIR/perf_monitor.py" --watch-pid "$$" \
      --label "gfs_incremental_cron" --log-dir "$EVU_PERF_LOG_DIR" \
      --interval "$EVU_PERF_SAMPLE_SECONDS" --retention-days "$EVU_PERF_RETENTION_DAYS" &
    GFS_PERF_MONITOR_PID=$!
  else
    echo "WARNING: GFS performance log directory unavailable: $EVU_PERF_LOG_DIR" >&2
  fi
fi
# GFS has an independent poller; prune its dated logs on every run instead
# of relying solely on the separate daily model-retention sweep.
# Failure to prune logs must not prevent forecast conversion.
if ! "$PYTHON_BIN" "$BASE_DIR/cleanup_cron_logs.py" \
    --logs-dir "$LOG_DIR" --retention-days "$RETENTION_DAYS" --prefix gfs_incremental; then
  echo 'WARNING: GFS cron log retention did not complete' >&2
fi
# Enumerate the latest possible finished cycle plus older cycles.  Missed and
# delayed runs are checked again on subsequent polls without resetting hours.
mapfile -t CYCLES < <("$PYTHON_BIN" - "$GFS_LAG_HOURS" "$GFS_CATCHUP_CYCLES" <<'PY'
import datetime as d,sys
lag=float(sys.argv[1]); back=int(sys.argv[2])
if not 0 <= lag <= 12 or not 0 <= back <= 20:raise SystemExit('GFS lag must be 0..12 and backfill 0..20 cycles')
now=d.datetime.now(d.timezone.utc)-d.timedelta(hours=lag)
start=now.replace(hour=(now.hour//6)*6, minute=0, second=0, microsecond=0)
for k in range(back+1):print((start-d.timedelta(hours=6*k)).strftime('%Y%m%d %H'))
PY
)
extra=()
if [[ "$NO_PUBLISH" == 1 ]]; then extra+=(--no-publish); fi
if [[ "$GFS_SKIP_PROFILES" == 1 ]]; then extra+=(--skip-profiles); fi
if [[ "$GFS_MARINE_ENABLED" != 1 ]]; then extra+=(--skip-marine); fi
status=0
for item in "${CYCLES[@]}"; do
  read -r date cycle <<<"$item"
  "$PYTHON_BIN" "$BASE_DIR/gfs_to_zarr.py" --date "$date" --cycle "$cycle" \
    --models "$GFS_MODELS" --workers "$GFS_WORKERS" \
    --max-hour-jobs "$GFS_MAX_HOUR_JOBS" --retry-failed-minutes "$GFS_RETRY_MINUTES" \
    --wgrib2 "$WGRIB2_BIN" --v17-base-dir "$GFS_V17_BASE_DIR" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --protected-cycles-file "$PROTECTED_CYCLES_FILE" --retention-days "$RETENTION_DAYS" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    "${extra[@]}" || status=1
done
printf 'GFS POLL END %s UTC status=%s\n' "$(date -u +%FT%TZ)" "$status"
exit "$status"
