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
PYTHON_BIN="${PYTHON_BIN:-python3}"
WGRIB2_BIN="${WGRIB2_BIN:-/apps/ops/prod/libs/intel/19.1.3.304/cray-mpich/8.1.4/wgrib2/3.8.0/bin/wgrib2}"
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

# RRFS can remain on your established standalone converter; these are the
# seven additional sources. Set MODELS=all to process RRFS here too, but do
# NOT run the original RRFS converter simultaneously on the same Zarr store.
MODELS="${MODELS:-refs_mean,refs_m001,refs_m002,refs_m003,refs_m004,refs_m005,hrrr}"
EXTRA=()
if [[ -n "$WGRIB2_BIN" ]]; then
  EXTRA+=(--wgrib2 "$WGRIB2_BIN")
fi

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
