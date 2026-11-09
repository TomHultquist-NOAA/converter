#!/usr/bin/env bash
# OMD EVU multi-source converter cron wrapper.
# Preconfigured processing paths. Adjust only if your installation location changes.
set -euo pipefail
# Give generated model output predictable readable permissions.
umask 022
# One OpenMP thread per wgrib2 subprocess avoids excessive nested threading.
# Parallelism is provided by WORKERS (separate forecast-hour processes).
export OMP_NUM_THREADS=1
export OMP_THREAD_LIMIT=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
BASE_DIR="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing"
OUTPUT_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr"
WORK_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work"

# Create a per-invocation log immediately, before environment, configuration,
# case-priority, or writer-lock checks. This makes skipped and failed launches
# visible instead of sending their only messages to cron's fixed stderr capture.
CRON_KIND="multisource"
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


# Cron does not inherit the interactive shell module hierarchy. Source the
# one-time captured, proven-good executable/library environment instead.
# Generate it with ./capture_converter_runtime.sh from your interactive shell.
CONVERTER_ENV_FILE="${CONVERTER_ENV_FILE:-$BASE_DIR/converter_cron_env.sh}"
if [[ ! -r "$CONVERTER_ENV_FILE" ]]; then
  echo "Missing cron environment file: $CONVERTER_ENV_FILE" >&2
  exit 2
fi
# Some environment/module initialization scripts reference optional unset vars.
set +u
source "$CONVERTER_ENV_FILE"
set -u

# No module command is invoked in cron. Use executable paths captured during
# a successful interactive session instead of rebuilding MODULEPATH in cron.
PYTHON_BIN="${PYTHON_BIN:-}"
WGRIB2_BIN="${WGRIB2_BIN:-}"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "No executable Python 3 found: PYTHON_BIN=$PYTHON_BIN" >&2
  exit 127
fi
if [[ -z "$WGRIB2_BIN" || ! -x "$WGRIB2_BIN" ]]; then
  echo "No executable wgrib2 found: WGRIB2_BIN=$WGRIB2_BIN" >&2
  exit 127
fi
if ! "$PYTHON_BIN" -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' ; then
  echo "Python >=3.10 required: $($PYTHON_BIN --version 2>&1)" >&2
  exit 127
fi
# `wgrib2 -version` can print a normal version yet return nonzero in some batch environments.
# Reject missing libraries, OpenMP errors, or any additional diagnostics.
WG_TEST_STATUS=0
WG_TEST_OUTPUT="$("$WGRIB2_BIN" -version 2>&1)" || WG_TEST_STATUS=$?
if [[ ! "$WG_TEST_OUTPUT" =~ ^[[:space:]]*[vV]?([0-9]+\.)+[0-9]+[[:space:]]*$ ]]; then
  echo "wgrib2 did not return a clean version string in cron: $WGRIB2_BIN" >&2
  echo "wgrib2 -version exit status: $WG_TEST_STATUS" >&2
  echo "Actual output from wgrib2 -version:" >&2
  printf '%s\n' "$WG_TEST_OUTPUT" >&2
  echo "OMP_NUM_THREADS=$OMP_NUM_THREADS; LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-<unset>}" >&2
  echo "Regenerate converter_runtime_paths.sh from the working interactive shell." >&2
  exit 127
fi
# Safe dry check: make NO downloads, conversions, SSH transfers, or cron logs.
if [[ "${CHECK_ENV_ONLY:-0}" == 1 ]]; then
  CRON_OUTCOME=check_only
  CRON_REASON=environment_preflight_only
  echo "Cron runtime OK: Python=$PYTHON_BIN ; wgrib2=$WGRIB2_BIN"
  exit 0
fi
WORKERS="${WORKERS:-16}"
# 16 subprocesses is a conservative fraction of a 256-logical-CPU node.
# WORKERS and ALIGNED_WORKERS remain user-overridable; no hard CPU quota is implied.
RETENTION_DAYS="${RETENTION_DAYS:-7}"
RRFS_RETENTION_DAYS="${RRFS_RETENTION_DAYS:-$RETENTION_DAYS}"
GFS_RETENTION_DAYS="${GFS_RETENTION_DAYS:-$RETENTION_DAYS}"
OBS_RETENTION_DAYS="${OBS_RETENTION_DAYS:-$RETENTION_DAYS}"
PROTECTED_CYCLES_FILE="${PROTECTED_CYCLES_FILE:-$BASE_DIR/protected_cycles.json}" # Copy .example.json once; never overwrite your list
# Legacy observation switches plus the shared converter-wide configuration.
# The same converter_config.sh is used by both cron workflows and create_case.py.
# Legacy non-secret observation settings are loaded first for migration.
# The shared operational config below is sourced afterward and takes precedence.
PTYPE_OBS_CONFIG_FILE="${PTYPE_OBS_CONFIG_FILE:-$BASE_DIR/ptype_obs_config.sh}"
if [[ -r "$PTYPE_OBS_CONFIG_FILE" ]]; then
  set +u
  source "$PTYPE_OBS_CONFIG_FILE"
  set -u
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
RETENTION_DAYS="${RETENTION_DAYS:-7}"
RRFS_RETENTION_DAYS="${RRFS_RETENTION_DAYS:-$RETENTION_DAYS}"
GFS_RETENTION_DAYS="${GFS_RETENTION_DAYS:-$RETENTION_DAYS}"
OBS_RETENTION_DAYS="${OBS_RETENTION_DAYS:-$RETENTION_DAYS}"
echo "Retention (days): RRFS=$RRFS_RETENTION_DAYS GFS=$GFS_RETENTION_DAYS OBS=$OBS_RETENTION_DAYS"
if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || (( RETENTION_DAYS < 1 || RETENTION_DAYS > 366 )); then
  echo "Invalid RETENTION_DAYS=${RETENTION_DAYS}; expected 1..366." >&2
  exit 2
fi
for period in "$RRFS_RETENTION_DAYS" "$GFS_RETENTION_DAYS" "$OBS_RETENTION_DAYS"; do
  if [[ ! "$period" =~ ^[0-9]+$ ]] || (( period < 1 || period > 366 )); then echo "Invalid retention period: $period" >&2; exit 2; fi
done
MODEL_SCHEDULER="${MODEL_SCHEDULER:-legacy}" # incremental disables the old four model starts
CYCLE_LAG_HOURS="${CYCLE_LAG_HOURS:-3.5}"  # New cycle eligible 210 min after initialization
# Include the newest eligible 6-hour cycle plus THREE prior cycles.
# This recovers cycles skipped while updating cron or individual missing models;
# the converter skips forecast hours already marked processed in each store.
CATCHUP_CYCLES="${CATCHUP_CYCLES:-3}" # 0=current only; 3=current + 18h historical recovery
# Run verification EVERY UTC hour; models only at 04/10/16/22 UTC.
# Verification is checked every hour. 20h includes all last-six-hour gaps
# and retains delayed URMA arrival (450 minutes) and other late products.
# Already completed valid hours are skipped by verification_pipeline.py.
VERIFY_LOOKBACK_HOURS="${VERIFY_LOOKBACK_HOURS:-20}"
MRMS_STRIDE="${MRMS_STRIDE:-1}"
VERIFY_ENABLED="${VERIFY_ENABLED:-1}"
MODEL_UTC_HOURS="${MODEL_UTC_HOURS:-04 10 16 22}"
VERIFY_ONLY="${VERIFY_ONLY:-0}"
MODELS_ONLY="${MODELS_ONLY:-0}"
# Use the exact publisher destination already configured in the model converter.
PUBLISH_HOST="${PUBLISH_HOST:-thultquist@rzdm.ncep.noaa.gov}"
PUBLISH_DIR="${PUBLISH_DIR:-/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data}"
NO_PUBLISH="${NO_PUBLISH:-0}"
PUBLISH_WORKERS="${PUBLISH_WORKERS:-3}"
export PUBLISH_WORKERS # Shared by model, verification, and aligned publication

if [[ "$BASE_DIR" == /PATH/* || "$OUTPUT_ROOT" == /PATH/* || "$WORK_ROOT" == /PATH/* ]]; then
  echo "Edit BASE_DIR, OUTPUT_ROOT, and WORK_ROOT first." >&2
  exit 2
fi
mkdir -p "$BASE_DIR" "$OUTPUT_ROOT" "$WORK_ROOT" "$BASE_DIR/logs"
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
# Prevent two cron runs from modifying the same cycle's stores/catalogs.
exec 9>"$BASE_DIR/.multisource_cron.lock"
if ! flock -n 9; then
  CRON_OUTCOME=skipped
  CRON_REASON=multisource_writer_lock_busy
  echo "Another converter is still running. This invocation is skipped."
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
      --label "multisource_cron" --log-dir "$EVU_PERF_LOG_DIR" \
      --interval "$EVU_PERF_SAMPLE_SECONDS" --retention-days "$EVU_PERF_RETENTION_DAYS" &
    PERF_MONITOR_PID=$!
  else
    echo "WARNING: performance log directory is unavailable; continuing without whole-cron sampling: $EVU_PERF_LOG_DIR"
  fi
fi
printf '\n=== %s UTC: unified model + verification processing started ===\n' "$(date -u +%FT%TZ)"
printf 'Host: %s | User: %s | PWD: %s\n' "$(hostname)" "$(id -un)" "$BASE_DIR"
printf 'Python executable: %s\n' "$PYTHON_BIN"
"$PYTHON_BIN" --version
printf 'wgrib2 executable: %s\n' "$WGRIB2_BIN"
printf 'PATH: %s\n' "$PATH"
if type module >/dev/null 2>&1; then module list 2>&1 || true; fi
cd "$BASE_DIR"

# Hourly verification and six-hourly models share this lock, working directory,
# loaded modules, and log. Run verification first so an unexpectedly slow
# model cycle does not always defer hourly analysis updates.
FAILED=0
EXTRA_COMMON=(
  --wgrib2 "$WGRIB2_BIN" --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT"
  --retention-days "$OBS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR"
)
if [[ "$NO_PUBLISH" == 1 ]]; then EXTRA_COMMON+=(--no-publish); fi
if [[ "$VERIFY_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  echo "== $(date -u +%FT%TZ) Hourly RAP/RTMA/URMA/MRMS verification =="
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --verification-only "${EXTRA_COMMON[@]}" \
    --lookback-hours "$VERIFY_LOOKBACK_HOURS" --mrms-stride "$MRMS_STRIDE" || FAILED=1
fi
NO_PUBLISH_ARG=()
if [[ "$NO_PUBLISH" == 1 ]]; then NO_PUBLISH_ARG+=(--no-publish); fi
# Alignment is incremental and independent: preserve native analysis stores,
# convert only new/updated valid hours to the shared model Lambert grid, then
# publish the aligned catalog after its stores. No extra cron entry required.
ALIGNED_ENABLED="${ALIGNED_ENABLED:-1}"
ALIGNED_MAX_NEW_HOURS="${ALIGNED_MAX_NEW_HOURS:-16}"
ALIGNED_WORKERS="${ALIGNED_WORKERS:-2}" # concurrent alignment jobs (any positive integer)
printf "Resource settings: model workers=%s, analysis-alignment workers=%s, publish workers=%s, OMP_NUM_THREADS=%s, nproc=%s\n" "$WORKERS" "$ALIGNED_WORKERS" "$PUBLISH_WORKERS" "$OMP_NUM_THREADS" "$(nproc)"
if [[ "$ALIGNED_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  echo "== $(date -u +%FT%TZ) Align verification fields to model grid =="
  "$PYTHON_BIN" "$BASE_DIR/aligned_analysis.py" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$OBS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    --max-new-hours "$ALIGNED_MAX_NEW_HOURS" \
    --workers "$ALIGNED_WORKERS" \
    "${NO_PUBLISH_ARG[@]}" || FAILED=1
fi

# Optional public precipitation-type observation overlays. API credentials are
# stored ONLY in a mode-0600, local shell file; never published or packaged.
# Synoptic/mPING public display must be authorized before enabling publishing.
PTYPE_OBS_ENABLED="${PTYPE_OBS_ENABLED:-0}"
PTYPE_OBS_PUBLISH="${PTYPE_OBS_PUBLISH:-0}"
PTYPE_OBS_RIGHTS_CONFIRMED="${PTYPE_OBS_RIGHTS_CONFIRMED:-0}"
PTYPE_OBS_SECRETS_FILE="${PTYPE_OBS_SECRETS_FILE:-$BASE_DIR/ptype_obs_secrets.sh}"
# NOAA SPC preliminary local storm reports, daily 12Z–12Z CSV files.
# Independent of licensed Synoptic/mPING data; no credentials required.
spc_pid=""
SPC_LSR_ENABLED="${SPC_LSR_ENABLED:-1}"
SPC_LSR_LOOKBACK_DAYS="${SPC_LSR_LOOKBACK_DAYS:-3}"
if [[ "$SPC_LSR_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  SPC_ARGS=(--output-root "$OUTPUT_ROOT" --lookback-days "$SPC_LSR_LOOKBACK_DAYS"
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR")
  [[ "$NO_PUBLISH" == 1 ]] || SPC_ARGS+=(--publish)
  echo "== $(date -u +%FT%TZ) SPC preliminary storm reports =="
  if [[ "${HOURLY_PARALLEL_REPORTS:-1}" == 1 && "$PTYPE_OBS_ENABLED" == 1 ]]; then
    # SPC and credentialed observations publish separate reports; wait for
    # both before any later legacy model work, retaining the existing lock.
    "$PYTHON_BIN" "$BASE_DIR/spc_lsr.py" "${SPC_ARGS[@]}" &
    spc_pid=$!
  else
    "$PYTHON_BIN" "$BASE_DIR/spc_lsr.py" "${SPC_ARGS[@]}" || FAILED=1
  fi
fi

if [[ "$PTYPE_OBS_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  if [[ -r "$PTYPE_OBS_SECRETS_FILE" ]]; then
    # No 'set -x' and no key values in logs, command line or environment dump.
    if [[ "$(stat -c %a "$PTYPE_OBS_SECRETS_FILE")" != "600" ]]; then
      echo "Observation credentials file must have mode 600: $PTYPE_OBS_SECRETS_FILE" >&2
      FAILED=1
    else
      set +u
      source "$PTYPE_OBS_SECRETS_FILE"
      set -u
      OBS_ARGS=(--output-root "$OUTPUT_ROOT" --lookback-hours "${PTYPE_OBS_LOOKBACK_HOURS:-3}"
        --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR")
      if [[ "$PTYPE_OBS_PUBLISH" == 1 && "$NO_PUBLISH" != 1 ]]; then
        if [[ "$PTYPE_OBS_RIGHTS_CONFIRMED" != 1 ]]; then
          echo "Public observation display not confirmed: refusing publication." >&2
          FAILED=1
        else
          OBS_ARGS+=(--publish --rights-confirmed)
        fi
      fi
      if [[ "$PTYPE_OBS_PUBLISH" != 1 || "$PTYPE_OBS_RIGHTS_CONFIRMED" == 1 || "$NO_PUBLISH" == 1 ]]; then
        echo "== $(date -u +%FT%TZ) Precipitation-type observations =="
        "$PYTHON_BIN" "$BASE_DIR/ptype_observations.py" "${OBS_ARGS[@]}" || FAILED=1
      fi
      unset SYNOPTIC_API_TOKEN MPING_API_KEY
    fi
  else
    echo "Precipitation-type observation credentials file not present; skipping." >&2
  fi
fi
# Optional, unauthenticated GOES GLM flash collector. This runs as a separate
# observation-only step: it never reprocesses model forecasts or alters catalogs.
# Enable in the shared converter_config.sh; default OFF.
GLM_ENABLED="${GLM_ENABLED:-0}"
GLM_LOOKBACK_HOURS="${GLM_LOOKBACK_HOURS:-3}"
GLM_WORKERS="${GLM_WORKERS:-4}"
GLM_PUBLISH="${GLM_PUBLISH:-1}"
if [[ "$GLM_ENABLED" != 0 && "$GLM_ENABLED" != 1 ]]; then
  echo "Invalid GLM_ENABLED=${GLM_ENABLED}; expected 0 or 1. Optional GLM collection skipped." >&2
elif [[ "$GLM_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  GLM_CONFIG_ERROR=""
  if [[ ! "$GLM_LOOKBACK_HOURS" =~ ^[0-9]+$ ]] || (( GLM_LOOKBACK_HOURS < 1 || GLM_LOOKBACK_HOURS > 24 )); then
    GLM_CONFIG_ERROR="GLM_LOOKBACK_HOURS must be 1..24"
  elif [[ ! "$GLM_WORKERS" =~ ^[0-9]+$ ]] || (( GLM_WORKERS < 1 || GLM_WORKERS > 8 )); then
    GLM_CONFIG_ERROR="GLM_WORKERS must be 1..8"
  elif [[ "$GLM_PUBLISH" != 0 && "$GLM_PUBLISH" != 1 ]]; then
    GLM_CONFIG_ERROR="GLM_PUBLISH must be 0 or 1"
  fi
  if [[ -n "$GLM_CONFIG_ERROR" ]]; then
    echo "${GLM_CONFIG_ERROR}; optional GLM collection skipped." >&2
  else
    GLM_ARGS=(--output-root "$OUTPUT_ROOT" --lookback-hours "$GLM_LOOKBACK_HOURS"
      --workers "$GLM_WORKERS" --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR")
    if [[ "$GLM_PUBLISH" == 1 && "$NO_PUBLISH" != 1 ]]; then GLM_ARGS+=(--publish); fi
    echo "== $(date -u +%FT%TZ) GLM lightning (observation-only) =="
    "$PYTHON_BIN" "$BASE_DIR/glm_lightning.py" "${GLM_ARGS[@]}" || {
      echo "Optional GLM collection failed (weather model processing continues)." >&2
    }
  fi
fi
# Finish both report publishers before subsequent legacy model work.
if [[ -n "$spc_pid" ]]; then
  wait "$spc_pid" || FAILED=1
fi


UTC_HOUR=$(date -u +%H)
DO_MODELS=0
if [[ "$VERIFY_ONLY" != 1 && "$MODEL_SCHEDULER" != incremental && " $MODEL_UTC_HOURS " == *" $UTC_HOUR "* ]]; then
  DO_MODELS=1
fi
if [[ "$DO_MODELS" != 1 ]]; then
  printf 'UTC hour %s: model runs skipped (scheduled: %s). Failed flag=%s\n' \
    "$UTC_HOUR" "$MODEL_UTC_HOURS" "$FAILED"
  exit "$FAILED"
fi

# Check the newest eligible UTC cycle and the previous 3 cycles (18 hours).
# Full or partial missed model runs are retried; forecast hours whose
# processed=1 marker is set are skipped. This includes day-boundary cycles.
# No third-party Python dependencies required.
CYCLE_CHOICES=$("$PYTHON_BIN" - "$CATCHUP_CYCLES" "$CYCLE_LAG_HOURS" <<'PY'
import datetime as dt, sys, math
back = int(sys.argv[1])
lag = float(sys.argv[2])
if not math.isfinite(lag) or not (0 <= lag <= 24):
    raise SystemExit("CYCLE_LAG_HOURS must be between 0 and 24")
if back < 0 or back > 3:
    raise SystemExit('CATCHUP_CYCLES must be 0-3')
now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lag)
start = now.replace(hour=(now.hour//6)*6, minute=0, second=0, microsecond=0)
for n in range(back+1):
    cycle = start - dt.timedelta(hours=6*n)
    print(cycle.strftime('%Y%m%d %H'))
PY
) || { echo "Cannot select UTC cycles: check CYCLE_LAG_HOURS" >&2; exit 2; }
mapfile -t CYCLES <<<"$CYCLE_CHOICES"
printf "UTC cycle eligibility lag: %s hours. Selected: %s\n" "$CYCLE_LAG_HOURS" "${CYCLES[*]}"

# Unified eight-source processing (RRFS, REFS mean, five REFS members, HRRR).
# Do NOT run the earlier standalone RRFS converter against the same Zarr store.
# Override MODELS to process any subset, e.g. MODELS=rrfs for RRFS backfill.
MODELS="${MODELS:-all}"
EXTRA=()
if [[ "$NO_PUBLISH" == 1 ]]; then EXTRA+=(--no-publish); fi
if [[ -n "$WGRIB2_BIN" ]]; then
  EXTRA+=(--wgrib2 "$WGRIB2_BIN")
fi

printf "Processing model selection: %s\n" "$MODELS"
for choice in "${CYCLES[@]}"; do
  read -r DATE CYCLE <<<"$choice"
  echo "== $(date -u +%FT%TZ) Running initialization $DATE $CYCLE UTC =="
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$DATE" --cycle "$CYCLE" \
    --models "$MODELS" --workers "$WORKERS" --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" --logs-dir "$BASE_DIR/logs" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    "${EXTRA[@]}" || FAILED=1
done
# REFS mean PMMN reflectivity and categorical fields can be published AFTER
# the base mean product is marked complete.  Revisit 3-4 day-old completed
# cycles without overwriting existing chunks.  Default is two cycles per model
# cron run (not an all-archive backfill).  Other models are untouched.
REFS_MEAN_LATE_REPAIR="${REFS_MEAN_LATE_REPAIR:-1}"
REFS_MEAN_REPAIR_LAGS_HOURS="${REFS_MEAN_REPAIR_LAGS_HOURS:-72 96}"
REFS_MEAN_REPAIR_WORKERS="${REFS_MEAN_REPAIR_WORKERS:-2}"
if [[ "$REFS_MEAN_LATE_REPAIR" == 1 ]]; then
  for lag in $REFS_MEAN_REPAIR_LAGS_HOURS; do
    if [[ ! "$lag" =~ ^[0-9]+$ ]] || (( lag < 48 || lag > 240 )); then
      echo "REFS mean repair lag must be 48-240 hours: $lag" >&2
      FAILED=1
      continue
    fi
    # Select the matching 6-hour UTC cycle exactly this many hours earlier.
    read -r REPAIR_DATE REPAIR_CYCLE < <("$PYTHON_BIN" - "$lag" <<'PYTHON'
import datetime as dt,sys
hour=int(sys.argv[1]);now=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=hour)
cycle=now.replace(hour=(now.hour//6)*6,minute=0,second=0,microsecond=0)
print(cycle.strftime('%Y%m%d %H'))
PYTHON
)
    REPAIR_STORE="$OUTPUT_ROOT/refs_mean_${REPAIR_DATE}_${REPAIR_CYCLE}00.zarr"
    if [[ ! -d "$REPAIR_STORE" ]]; then
      echo "Skip late REFS mean repair: store absent $REPAIR_STORE"
      continue
    fi
    echo "== $(date -u +%FT%TZ) Repair late REFS mean fields $REPAIR_DATE $REPAIR_CYCLE UTC =="
    "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
      --date "$REPAIR_DATE" --cycle "$REPAIR_CYCLE" --models refs_mean \
      --repair-refs-mean --workers "$REFS_MEAN_REPAIR_WORKERS" --obs-retention-days "$OBS_RETENTION_DAYS" --gfs-retention-days "$GFS_RETENTION_DAYS" \
      --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$RRFS_RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      "${EXTRA[@]}" || FAILED=1
  done
fi

printf '=== %s UTC: completed; failed_flag=%s ===\n' "$(date -u +%FT%TZ)" "$FAILED"
exit "$FAILED"
