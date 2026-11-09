#!/usr/bin/env bash
# OMD EVU v1.4.0: incremental models and bounded overlapping sounding-profile collection.
# Run every 15 minutes alongside the existing hourly verification/obs cron.
# This watcher only writes model Zarr stores and model-specific catalogs.
set -euo pipefail
umask 022
export OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
BASE_DIR="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing"
OUTPUT_ROOT="$BASE_DIR/zarr"
WORK_ROOT="$BASE_DIR/work"

# Create a per-invocation log immediately, before environment, configuration,
# case-priority, or writer-lock checks. This makes skipped and failed launches
# visible instead of sending their only messages to cron's fixed stderr capture.
CRON_KIND="model_incremental"
CRON_START_EPOCH=$(date +%s)
CRON_OUTCOME=running
CRON_REASON=none
PERF_MONITOR_PID=""
LOGFILE=""
if mkdir -p "$BASE_DIR/logs" 2>/dev/null; then
  LOGFILE="$BASE_DIR/logs/${CRON_KIND}_$(date -u +%Y%m%d_%H%M%S)_$$.log"
  if ! ( : >> "$LOGFILE" ) 2>/dev/null; then LOGFILE=""; fi
fi
if [[ -n "$LOGFILE" ]]; then
  exec >>"$LOGFILE" 2>&1
fi
cron_exit_trap() {
  local rc=$?
  trap - EXIT
  if [[ -n "${PERF_MONITOR_PID:-}" ]]; then
    kill -TERM "$PERF_MONITOR_PID" 2>/dev/null || true
    wait "$PERF_MONITOR_PID" 2>/dev/null || true
  fi
  local finish_epoch elapsed outcome
  finish_epoch=$(date +%s)
  elapsed=$((finish_epoch-CRON_START_EPOCH))
  outcome="$CRON_OUTCOME"
  if [[ "$outcome" == running ]]; then
    if (( rc == 0 )); then outcome=completed; else outcome=failed; fi
  fi
  if [[ -n "$LOGFILE" ]]; then
    printf '=== INVOCATION END %s UTC outcome=%s exit_status=%s elapsed_seconds=%s reason=%s pid=%s ===\n' \
      "$(date -u +%FT%TZ)" "$outcome" "$rc" "$elapsed" "$CRON_REASON" "$$" || true
  fi
  exit "$rc"
}
trap cron_exit_trap EXIT
printf '=== INVOCATION START %s UTC cron=%s pid=%s host=%s user=%s ===\n' \
  "$(date -u +%FT%TZ)" "$CRON_KIND" "$$" "$(hostname 2>/dev/null || echo unknown)" "$(id -un 2>/dev/null || echo unknown)"

CONVERTER_ENV_FILE="${CONVERTER_ENV_FILE:-$BASE_DIR/converter_cron_env.sh}"
if [[ ! -r "$CONVERTER_ENV_FILE" ]]; then
  echo "Missing runtime environment: $CONVERTER_ENV_FILE" >&2; exit 2
fi
set +u; source "$CONVERTER_ENV_FILE"; set -u
PYTHON_BIN="${PYTHON_BIN:-}"
WGRIB2_BIN="${WGRIB2_BIN:-}"
if [[ ! -x "$PYTHON_BIN" || ! -x "$WGRIB2_BIN" ]]; then
  echo 'Missing captured Python/wgrib2 executable; regenerate converter runtime' >&2; exit 127
fi
# The shared operational configuration controls both cron workflows and
# historical case processing (models, soundings, analyses, observations,
# GLM, publication, retention, and CASE_* worker overrides).
# INCREMENTAL_CONFIG_FILE remains a deprecated environment-variable alias.
CONFIG_OVERRIDE=""
if [[ -n "${CONVERTER_CONFIG_FILE:-}" ]]; then
  CONFIG_OVERRIDE="$CONVERTER_CONFIG_FILE"
elif [[ -n "${INCREMENTAL_CONFIG_FILE:-}" ]]; then
  CONFIG_OVERRIDE="$INCREMENTAL_CONFIG_FILE"
  echo "NOTICE: INCREMENTAL_CONFIG_FILE is deprecated; use CONVERTER_CONFIG_FILE." >&2
fi
if [[ -n "$CONFIG_OVERRIDE" ]]; then
  CONVERTER_CONFIG_FILE="$CONFIG_OVERRIDE"
else
  CONVERTER_CONFIG_FILE="$BASE_DIR/converter_config.sh"
  # One-release migration fallback: continue reading the old filename if the
  # new canonical file has not yet been renamed on the installation.
  if [[ ! -r "$CONVERTER_CONFIG_FILE" && -r "$BASE_DIR/incremental_model_config.sh" ]]; then
    CONVERTER_CONFIG_FILE="$BASE_DIR/incremental_model_config.sh"
    echo "NOTICE: Using legacy incremental_model_config.sh; rename it to converter_config.sh." >&2
  fi
fi
if [[ -r "$CONVERTER_CONFIG_FILE" ]]; then
  set +u
  source "$CONVERTER_CONFIG_FILE"
  set -u
elif [[ -n "$CONFIG_OVERRIDE" ]]; then
  echo "Configured converter settings file is not readable: $CONVERTER_CONFIG_FILE" >&2
  exit 2
fi
# Operational RRFS/REFS: begin polling the latest cycle after 15 minutes.
# Missing GRIB indexes/products are retried on later polls; no assumed completeness.
INCREMENTAL_CYCLE_LAG_HOURS="${INCREMENTAL_CYCLE_LAG_HOURS:-0.25}"
INCREMENTAL_CATCHUP_CYCLES="${INCREMENTAL_CATCHUP_CYCLES:-3}"
INCREMENTAL_LOOKAHEAD="${INCREMENTAL_LOOKAHEAD:-12}"
INCREMENTAL_MAX_HOUR_JOBS="${INCREMENTAL_MAX_HOUR_JOBS:-0}"
INCREMENTAL_MISSING_RETRY_MINUTES="${INCREMENTAL_MISSING_RETRY_MINUTES:-60}"
INCREMENTAL_WORKERS="${INCREMENTAL_WORKERS:-12}"
# Conversion and profile generation use distinct stores/catalogs. Set 0 to
# revert to the previous serial order if the node is resource constrained.
INCREMENTAL_PARALLEL_STAGES="${INCREMENTAL_PARALLEL_STAGES:-1}"
# Keep wgrib2 OpenMP single-threaded; cap concurrent model-hour tasks.
MODELS="${MODELS:-all}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
RRFS_RETENTION_DAYS="${RRFS_RETENTION_DAYS:-$RETENTION_DAYS}"
GFS_RETENTION_DAYS="${GFS_RETENTION_DAYS:-$RETENTION_DAYS}"
OBS_RETENTION_DAYS="${OBS_RETENTION_DAYS:-$RETENTION_DAYS}"
for entry in "RRFS:$RRFS_RETENTION_DAYS" "GFS:$GFS_RETENTION_DAYS" "OBS:$OBS_RETENTION_DAYS"; do
  period="${entry#*:}"
  if [[ ! "$period" =~ ^[0-9]+$ ]] || (( period < 1 || period > 366 )); then echo "Invalid retention policy: $entry (1..366 required)" >&2; exit 2; fi
done
echo "Retention (days): RRFS/REFS/HRRR=$RRFS_RETENTION_DAYS GFS=$GFS_RETENTION_DAYS OBS=$OBS_RETENTION_DAYS"
PUBLISH_HOST="${PUBLISH_HOST:-thultquist@rzdm.ncep.noaa.gov}"
PUBLISH_DIR="${PUBLISH_DIR:-/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data}"
PROTECTED_CYCLES_FILE="${PROTECTED_CYCLES_FILE:-$BASE_DIR/protected_cycles.json}"
NO_PUBLISH="${NO_PUBLISH:-0}"
PUBLISH_WORKERS="${PUBLISH_WORKERS:-3}"
export PUBLISH_WORKERS
mkdir -p "$BASE_DIR/logs" "$OUTPUT_ROOT" "$WORK_ROOT"
# Case/cron coordination: both cron types may run at the same time, but
# a waiting or active historical case has priority over NEW scheduled runs.
CASE_GATE_HELPER="$BASE_DIR/case_cron_coordination.sh"
if [[ ! -r "$CASE_GATE_HELPER" ]]; then
  echo "Missing case/cron coordination helper: $CASE_GATE_HELPER" >&2
  exit 2
fi
source "$CASE_GATE_HELPER"
if ! acquire_cron_case_gate "$BASE_DIR"; then
  CRON_OUTCOME=skipped
  CRON_REASON="${CRON_SKIP_REASON:-case_priority_gate}"
  exit 0
fi
# Independent model lock: verification and observations may run concurrently
# (different stores), while two model writers cannot collide.
exec 9>"$BASE_DIR/.model_incremental.lock"
if ! flock -n 9; then
  CRON_OUTCOME=skipped
  CRON_REASON=model_incremental_writer_lock_busy
  echo 'Incremental model poll skipped; previous model invocation still active.'
  exit 0
fi
# Whole-cron process-tree sampler complements per-command stage/task events.
# It excludes its own helper PID and is stopped cleanly by the EXIT trap.
EVU_PERF_ENABLED="${EVU_PERF_ENABLED:-0}"
EVU_PERF_SAMPLE_SECONDS="${EVU_PERF_SAMPLE_SECONDS:-5}"
EVU_PERF_RETENTION_DAYS="${EVU_PERF_RETENTION_DAYS:-$RETENTION_DAYS}"
# Retain no performance diagnostics longer than the configured data window.
if [[ "$EVU_PERF_RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( EVU_PERF_RETENTION_DAYS > RETENTION_DAYS )); then
  EVU_PERF_RETENTION_DAYS="$RETENTION_DAYS"
fi
EVU_PERF_LOG_DIR="${EVU_PERF_LOG_DIR:-$BASE_DIR/logs/performance}"
export EVU_PERF_ENABLED EVU_PERF_SAMPLE_SECONDS EVU_PERF_RETENTION_DAYS EVU_PERF_LOG_DIR
if [[ "$EVU_PERF_ENABLED" == 1 && -r "$BASE_DIR/perf_monitor.py" ]]; then
  if mkdir -p "$EVU_PERF_LOG_DIR" 2>/dev/null; then
    "$PYTHON_BIN" "$BASE_DIR/perf_monitor.py" --watch-pid "$$" \
      --label "model_incremental_cron" --log-dir "$EVU_PERF_LOG_DIR" \
      --interval "$EVU_PERF_SAMPLE_SECONDS" --retention-days "$EVU_PERF_RETENTION_DAYS" &
    PERF_MONITOR_PID=$!
  else
    echo "WARNING: performance log directory is unavailable; continuing without whole-cron sampling: $EVU_PERF_LOG_DIR"
  fi
fi
cd "$BASE_DIR"
mapfile -t CYCLES < <("$PYTHON_BIN" - "$INCREMENTAL_CYCLE_LAG_HOURS" "$INCREMENTAL_CATCHUP_CYCLES" <<'PYCYCLES'
import datetime as dt,math,sys
lag=float(sys.argv[1]);back=int(sys.argv[2])
if not math.isfinite(lag) or not 0<=lag<=6 or not 0<=back<=3:
    raise SystemExit('Invalid incremental lag (0-6) or back cycles (0-3)')
now=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=lag)
latest=now.replace(hour=(now.hour//6)*6,minute=0,second=0,microsecond=0)
for i in range(back+1):
    print((latest-dt.timedelta(hours=6*i)).strftime('%Y%m%d %H'))
PYCYCLES
)
extra=()
if [[ "$NO_PUBLISH" == 1 ]]; then extra+=(--no-publish); fi
status=0
printf 'Cycle discovery lag: %s hours; missing-data retry: %s minutes\n' "$INCREMENTAL_CYCLE_LAG_HOURS" "$INCREMENTAL_MISSING_RETRY_MINUTES"
printf '=== %s UTC incremental model poll: %s ===\n' "$(date -u +%FT%TZ)" "${CYCLES[*]}"
printf "Resource settings: incremental workers=%s, publish workers=%s, parallel stages=%s, OMP_NUM_THREADS=%s, nproc=%s\n" "$INCREMENTAL_WORKERS" "$PUBLISH_WORKERS" "$INCREMENTAL_PARALLEL_STAGES" "$OMP_NUM_THREADS" "$(nproc)"
for choice in "${CYCLES[@]}"; do
  read -r date cycle <<<"$choice"
  echo "=== $(date -u +%FT%TZ) $date $cycle UTC ==="
  model_cmd=("$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$date" --cycle "$cycle" --models "$MODELS" \
    --incremental --lookahead-hours "$INCREMENTAL_LOOKAHEAD" \
    --max-hour-jobs "$INCREMENTAL_MAX_HOUR_JOBS" \
    --missing-retry-minutes "$INCREMENTAL_MISSING_RETRY_MINUTES" \
    --workers "$INCREMENTAL_WORKERS" --wgrib2 "$WGRIB2_BIN" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" \
    --publish-mode auto --no-local-cleanup --no-remote-cleanup \
    "${extra[@]}")
  model_pid=""
  if [[ "$INCREMENTAL_PARALLEL_STAGES" == 1 && "${SOUNDING_PROFILES_ENABLED:-1}" == 1 ]]; then
    # Only one model writer and one profile writer at once. Wait for BOTH
    # before advancing to the next cycle (shared catalog single-writer rule).
    "${model_cmd[@]}" &
    model_pid=$!
  else
    "${model_cmd[@]}" || status=1
  fi
  # Operational sounding pipeline: bounded work and separate 12-km Zarr
  # stores. Enabled by default; disable with SOUNDING_PROFILES_ENABLED=0.
  # REFS has fewer levels: include one member each poll to bound data load.
  if [[ "${SOUNDING_PROFILES_ENABLED:-1}" == 1 ]]; then
    echo "=== Sounding profile poll $date $cycle ==="
    # Rotate through the five REFS members across successive quarter-hour
    # polls, rather than downloading every ensemble member simultaneously.
    # Forecast-hour backfill is bounded to protect normal model ingest.
    profiles="${SOUNDING_PROFILE_MODELS:-rrfs,hrrr}"
    if [[ "${SOUNDING_PROFILE_REFS_ENABLED:-1}" == 1 ]]; then
      member_index=$(( ($(date -u +%s) / 900) % 5 + 1 ))
      printf -v selected_member 'refs_m%03d' "$member_index"
      profiles="${profiles},${selected_member}"
    fi
    "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
      --date "$date" --cycle "$cycle" \
      --models "$profiles" \
      --max-hours-per-model "${SOUNDING_PROFILE_MAX_HOURS:-1}" \
      --hour-workers "${SOUNDING_PROFILE_HOUR_WORKERS:-2}" \
      --grid-stride "${SOUNDING_PROFILE_GRID_STRIDE:-4}" \
      --wgrib2 "$WGRIB2_BIN" --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      "${extra[@]}" || status=1
    # REFS Mean GRIB2 contains native T, Td and wind profiles from the
    # full operational ensemble, including time-lagged contributors.
    if [[ "${SOUNDING_PROFILE_REFS_MEAN_ENABLED:-1}" == 1 ]]; then
      echo "=== REFS Mean sounding profile $date $cycle ==="
      "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
        --date "$date" --cycle "$cycle" --models refs_mean \
        --max-hours-per-model "${SOUNDING_PROFILE_MAX_HOURS:-1}" \
        --hour-workers "${SOUNDING_PROFILE_HOUR_WORKERS:-2}" \
        --grid-stride "${SOUNDING_PROFILE_GRID_STRIDE:-4}" \
        --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
        --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
        --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
        "${extra[@]}" || status=1
    fi
  fi
  if [[ -n "$model_pid" ]]; then
    wait "$model_pid" || status=1
  fi
done
# RAP analyses are hourly, including nonsynoptic hours. Sample the most
# recent finished analysis (two-hour publication buffer) and its predecessor;
# repeated polls skip completed .hours markers. Errors do not stop map ingest.
if [[ "${SOUNDING_PROFILES_ENABLED:-1}" == 1 && "${SOUNDING_PROFILE_RAP_ENABLED:-1}" == 1 ]]; then
  for lag in 2 3; do
    read -r rap_date rap_cycle < <("$PYTHON_BIN" - "$lag" <<'PYRAP'
import datetime as dt,sys
stamp=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=int(sys.argv[1]))
print(stamp.strftime('%Y%m%d %H'))
PYRAP
)
    echo "=== RAP analysis sounding profile $rap_date ${rap_cycle}Z ==="
    "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
      --date "$rap_date" --cycle "$rap_cycle" --models rap_analysis --hours 0 \
      --max-hours-per-model 1 --grid-stride "${SOUNDING_PROFILE_RAP_STRIDE:-1}" \
      --wgrib2 "$WGRIB2_BIN" --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$OBS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      "${extra[@]}" || status=1
  done
fi
# REFS Mean PMM and ensemble products sometimes arrive well after the
# original run. Keep the proven 72/96-hour recovery behavior independently
# of the old model cron (disabled by MODEL_SCHEDULER=incremental).
UTC_HOUR=$(date -u +%H)
UTC_MINUTE=$(date -u +%M)
if [[ "$UTC_MINUTE" == 00 && " 04 10 16 22 " == *" $UTC_HOUR "* ]]; then
  for lag in 72 96; do
    read -r date cycle < <("$PYTHON_BIN" - "$lag" <<'PYLATE'
import datetime as dt,sys
now=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=int(sys.argv[1]))
t=now.replace(hour=(now.hour//6)*6,minute=0,second=0,microsecond=0)
print(t.strftime('%Y%m%d %H'))
PYLATE
)
    if [[ -d "$OUTPUT_ROOT/refs_mean_${date}_${cycle}00.zarr" ]]; then
      echo "=== REFS Mean late repair $date $cycle ==="
      "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
        --date "$date" --cycle "$cycle" --models refs_mean \
        --repair-refs-mean --workers 2 --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" --wgrib2 "$WGRIB2_BIN" \
        --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
        --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
        --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
        --no-local-cleanup --no-remote-cleanup \
        "${extra[@]}" || status=1
    fi
  done
fi
# Start routine retention after 03 UTC, but retry at later polls until it
# COMPLETES. An exact 03:00 test silently missed whole days whenever that poll
# was skipped by case priority, an overlapping writer, or a cron schedule that
# did not include 03:00. Mark success only after profile + model cleanup (and
# publication catalog refresh) complete; keep the last-success marker local.
RETENTION_DAY=$(date -u +%Y%m%d)
RETENTION_HOUR=$(date -u +%H)
RETENTION_MARKER="$BASE_DIR/.model_retention_last_success"
# Include the active retention period in the success marker. Otherwise a
# change from e.g. seven to four days after today's sweep is silently ignored
# until the next UTC date, even though the new policy has not run remotely.
# Legacy date-only markers intentionally trigger one new sweep.
RETENTION_POLICY_KEY="${RETENTION_DAY}:rrfs=${RRFS_RETENTION_DAYS}:obs=${OBS_RETENTION_DAYS}:gfs=${GFS_RETENTION_DAYS}"
RETENTION_LAST=$(cat "$RETENTION_MARKER" 2>/dev/null || true)
if (( 10#$RETENTION_HOUR >= 3 )) && [[ "$RETENTION_LAST" != "$RETENTION_POLICY_KEY" ]]; then
  echo "=== Daily RRFS ${RRFS_RETENTION_DAYS}-day, OBS ${OBS_RETENTION_DAYS}-day retention and catalog refresh: $RETENTION_DAY UTC ==="
  retention_ok=1
  if [[ "${SOUNDING_PROFILES_ENABLED:-1}" == 1 ]]; then
    "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
      --date "$(date -u +%Y%m%d)" --cycle 00 --models rrfs \
      --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      --cleanup-only "${extra[@]}" || { status=1; retention_ok=0; }
  fi
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$(date -u +%Y%m%d)" --cycle 00 --models all --cleanup-only --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" --logs-dir "$BASE_DIR/logs" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    "${extra[@]}" || { status=1; retention_ok=0; }
  if [[ "$NO_PUBLISH" != 1 ]]; then
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$(date -u +%Y%m%d)" --cycle 00 --models all --publish-only --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    --no-local-cleanup --no-remote-cleanup \
    "${extra[@]}" || { status=1; retention_ok=0; }
  fi
  if (( retention_ok == 1 )) && [[ "$NO_PUBLISH" != 1 ]]; then
    retention_tmp="$RETENTION_MARKER.$$"
    if printf '%s\n' "$RETENTION_POLICY_KEY" > "$retention_tmp" && mv -f "$retention_tmp" "$RETENTION_MARKER"; then
      echo "Daily retention completed successfully: $RETENTION_POLICY_KEY"
    else
      rm -f "$retention_tmp"
      echo 'ERROR: Could not persist daily retention success marker; will retry next poll.' >&2
      status=1
    fi
  elif (( retention_ok == 1 )); then
    echo 'Local retention completed; publication disabled, so remote cleanup will retry when enabled.'
  else
    echo 'WARNING: Daily retention incomplete; next eligible model poll will retry.' >&2
  fi
fi
printf '=== incremental model poll complete, status=%s ===\n' "$status"
exit "$status"
