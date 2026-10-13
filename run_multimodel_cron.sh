#!/usr/bin/env bash
# OMD EVU multi-source converter cron wrapper (WCOSS2).
# Preconfigured WCOSS2 paths. Adjust only if your installation location changes.
set -euo pipefail
# Give generated model output predictable readable permissions.
umask 022
# One OpenMP thread per wgrib2 subprocess avoids WCOSS2 OMP Error #34.
# Parallelism is provided by WORKERS (separate forecast-hour processes).
export OMP_NUM_THREADS=1
export OMP_THREAD_LIMIT=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
BASE_DIR="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing"
OUTPUT_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr"
WORK_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work"

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
# `wgrib2 -version` can print a normal version yet return nonzero on WCOSS2.
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
  echo "Cron runtime OK: Python=$PYTHON_BIN ; wgrib2=$WGRIB2_BIN"
  exit 0
fi
WORKERS="${WORKERS:-8}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"  # Same age limit on WCOSS2 and RZDM
CYCLE_LAG_HOURS="${CYCLE_LAG_HOURS:-3.5}"  # New cycle eligible 210 min after initialization
CATCHUP_CYCLES="${CATCHUP_CYCLES:-1}" # 0=current only; 1=current+previous 6h cycle
# Run verification EVERY UTC hour; models only at 04/10/16/22 UTC.
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

if [[ "$BASE_DIR" == /PATH/* || "$OUTPUT_ROOT" == /PATH/* || "$WORK_ROOT" == /PATH/* ]]; then
  echo "Edit BASE_DIR, OUTPUT_ROOT, and WORK_ROOT first." >&2
  exit 2
fi
mkdir -p "$BASE_DIR" "$OUTPUT_ROOT" "$WORK_ROOT" "$BASE_DIR/logs"
# Prevent two cron runs from modifying the same cycle's stores/catalogs.
exec 9>"$BASE_DIR/.multisource_cron.lock"
if ! flock -n 9; then
  echo "Another converter is still running. This invocation is skipped."
  exit 0
fi
LOGFILE="$BASE_DIR/logs/multisource_$(date -u +%Y%m%d_%H%M%S).log"
exec >>"$LOGFILE" 2>&1
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
  --retention-days "$RETENTION_DAYS" --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR"
)
if [[ "$NO_PUBLISH" == 1 ]]; then EXTRA_COMMON+=(--no-publish); fi
if [[ "$VERIFY_ENABLED" == 1 && "$MODELS_ONLY" != 1 ]]; then
  echo "== $(date -u +%FT%TZ) Hourly RAP/URMA/MRMS verification =="
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --verification-only "${EXTRA_COMMON[@]}" \
    --lookback-hours "$VERIFY_LOOKBACK_HOURS" --mrms-stride "$MRMS_STRIDE" || FAILED=1
fi
UTC_HOUR=$(date -u +%H)
DO_MODELS=0
if [[ "$VERIFY_ONLY" != 1 && " $MODEL_UTC_HOURS " == *" $UTC_HOUR "* ]]; then
  DO_MODELS=1
fi
if [[ "$DO_MODELS" != 1 ]]; then
  printf 'UTC hour %s: model runs skipped (scheduled: %s). Failed flag=%s\n' \
    "$UTC_HOUR" "$MODEL_UTC_HOURS" "$FAILED"
  exit "$FAILED"
fi

# Run the newest cycle available after the configured delay.
# Then revisit one previous cycle to recover data not published in time for
# its original run. All processed forecast hours are skipped automatically.
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
    --models "$MODELS" --workers "$WORKERS" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RETENTION_DAYS" --logs-dir "$BASE_DIR/logs" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    "${EXTRA[@]}" || FAILED=1
done
printf '=== %s UTC: completed; failed_flag=%s ===\n' "$(date -u +%FT%TZ)" "$FAILED"
exit "$FAILED"
