#!/usr/bin/env bash
# OMD EVU multi-source converter cron wrapper (WCOSS2).
# Preconfigured WCOSS2 paths. Adjust only if your installation location changes.
set -euo pipefail
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

# Cron does not inherit the interactive shell's loaded modules. Initialize
# only what the converter needs; do not spawn a login shell or source .bashrc.
# Errors here appear in the crontab's cron_errors.log redirection.
CONVERTER_ENV_FILE="${CONVERTER_ENV_FILE:-$BASE_DIR/converter_cron_env.sh}"
if [[ ! -r "$CONVERTER_ENV_FILE" ]]; then
  echo "Missing cron environment file: $CONVERTER_ENV_FILE" >&2
  exit 2
fi
# Some environment/module initialization scripts reference optional unset vars.
set +u
source "$CONVERTER_ENV_FILE"
set -u

# Prefer executables from the modules loaded above, not an unrelated
# hard-coded MPI/wgrib2 build. Explicit overrides still work.
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
WGRIB2_BIN="${WGRIB2_BIN:-$(command -v wgrib2 || true)}"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "No executable Python 3 found: PYTHON_BIN=$PYTHON_BIN" >&2
  exit 127
fi
if [[ -z "$WGRIB2_BIN" || ! -x "$WGRIB2_BIN" ]]; then
  echo "No executable wgrib2 found: WGRIB2_BIN=$WGRIB2_BIN" >&2
  exit 127
fi
WORKERS="${WORKERS:-8}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"  # Same age limit on WCOSS2 and RZDM
CYCLE_LAG_HOURS="${CYCLE_LAG_HOURS:-3.5}"  # New cycle eligible 210 min after initialization
CATCHUP_CYCLES="${CATCHUP_CYCLES:-1}" # 0=current only; 1=current+previous 6h cycle

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
printf '\n=== %s UTC: multi-source processing started ===\n' "$(date -u +%FT%TZ)"
printf 'Host: %s | User: %s | PWD: %s\n' "$(hostname)" "$(id -un)" "$BASE_DIR"
printf 'Python executable: %s\n' "$PYTHON_BIN"
"$PYTHON_BIN" --version
printf 'wgrib2 executable: %s\n' "$WGRIB2_BIN"
printf 'PATH: %s\n' "$PATH"
if type module >/dev/null 2>&1; then module list 2>&1 || true; fi
cd "$BASE_DIR"

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
if [[ -n "$WGRIB2_BIN" ]]; then
  EXTRA+=(--wgrib2 "$WGRIB2_BIN")
fi

printf "Processing model selection: %s\n" "$MODELS"
FAILED=0
for choice in "${CYCLES[@]}"; do
  read -r DATE CYCLE <<<"$choice"
  echo "== $(date -u +%FT%TZ) Running initialization $DATE $CYCLE UTC =="
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$DATE" --cycle "$CYCLE" \
    --models "$MODELS" --workers "$WORKERS" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RETENTION_DAYS" --logs-dir "$BASE_DIR/logs" \
    "${EXTRA[@]}" || FAILED=1
done
printf '=== %s UTC: completed; failed_flag=%s ===\n' "$(date -u +%FT%TZ)" "$FAILED"
exit "$FAILED"
