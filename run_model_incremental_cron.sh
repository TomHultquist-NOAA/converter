#!/usr/bin/env bash
# OMD EVU v1.3.1: incremental models plus bounded native sounding-profile collection.
# Run every 15 minutes alongside the existing hourly verification/obs cron.
# This watcher only writes model Zarr stores and model-specific catalogs.
set -euo pipefail
umask 022
export OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
BASE_DIR="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing"
OUTPUT_ROOT="$BASE_DIR/zarr"
WORK_ROOT="$BASE_DIR/work"
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
INCREMENTAL_CONFIG_FILE="${INCREMENTAL_CONFIG_FILE:-$BASE_DIR/incremental_model_config.sh}"
if [[ -r "$INCREMENTAL_CONFIG_FILE" ]]; then
  set +u; source "$INCREMENTAL_CONFIG_FILE"; set -u
fi
INCREMENTAL_CYCLE_LAG_HOURS="${INCREMENTAL_CYCLE_LAG_HOURS:-0.75}"
INCREMENTAL_CATCHUP_CYCLES="${INCREMENTAL_CATCHUP_CYCLES:-3}"
INCREMENTAL_LOOKAHEAD="${INCREMENTAL_LOOKAHEAD:-12}"
INCREMENTAL_MAX_HOUR_JOBS="${INCREMENTAL_MAX_HOUR_JOBS:-48}"
INCREMENTAL_MISSING_RETRY_MINUTES="${INCREMENTAL_MISSING_RETRY_MINUTES:-60}"
INCREMENTAL_WORKERS="${INCREMENTAL_WORKERS:-12}"
# Keep wgrib2 OpenMP single-threaded; cap concurrent model-hour tasks.
MODELS="${MODELS:-all}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
PUBLISH_HOST="${PUBLISH_HOST:-thultquist@rzdm.ncep.noaa.gov}"
PUBLISH_DIR="${PUBLISH_DIR:-/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data}"
PROTECTED_CYCLES_FILE="${PROTECTED_CYCLES_FILE:-$BASE_DIR/protected_cycles.json}"
NO_PUBLISH="${NO_PUBLISH:-0}"
mkdir -p "$BASE_DIR/logs" "$OUTPUT_ROOT" "$WORK_ROOT"
# Independent model lock: verification and observations may run concurrently
# (different stores), while two model writers cannot collide.
exec 9>"$BASE_DIR/.model_incremental.lock"
if ! flock -n 9; then
  echo 'Incremental model poll skipped; previous model invocation still active.'
  exit 0
fi
LOGFILE="$BASE_DIR/logs/model_incremental_$(date -u +%Y%m%d_%H%M%S).log"
exec >>"$LOGFILE" 2>&1
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
printf '=== %s UTC incremental model poll: %s ===\n' "$(date -u +%FT%TZ)" "${CYCLES[*]}"
printf "Resource settings: incremental workers=%s, OMP_NUM_THREADS=%s, nproc=%s\n" "$INCREMENTAL_WORKERS" "$OMP_NUM_THREADS" "$(nproc)"
for choice in "${CYCLES[@]}"; do
  read -r date cycle <<<"$choice"
  echo "=== $(date -u +%FT%TZ) $date $cycle UTC ==="
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$date" --cycle "$cycle" --models "$MODELS" \
    --incremental --lookahead-hours "$INCREMENTAL_LOOKAHEAD" \
    --max-hour-jobs "$INCREMENTAL_MAX_HOUR_JOBS" \
    --missing-retry-minutes "$INCREMENTAL_MISSING_RETRY_MINUTES" \
    --workers "$INCREMENTAL_WORKERS" --wgrib2 "$WGRIB2_BIN" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    --publish-mode auto --no-local-cleanup --no-remote-cleanup \
    "${extra[@]}" || status=1
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
      --grid-stride "${SOUNDING_PROFILE_GRID_STRIDE:-4}" \
      --wgrib2 "$WGRIB2_BIN" --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      "${extra[@]}" || status=1
    # REFS Mean GRIB2 contains native T, Td and wind profiles from the
    # full operational ensemble, including time-lagged contributors.
    if [[ "${SOUNDING_PROFILE_REFS_MEAN_ENABLED:-1}" == 1 ]]; then
      echo "=== REFS Mean sounding profile $date $cycle ==="
      "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
        --date "$date" --cycle "$cycle" --models refs_mean \
        --max-hours-per-model "${SOUNDING_PROFILE_MAX_HOURS:-1}" \
        --grid-stride "${SOUNDING_PROFILE_GRID_STRIDE:-4}" \
        --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
        --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
        --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
        "${extra[@]}" || status=1
    fi
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
      --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
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
        --repair-refs-mean --workers 2 --wgrib2 "$WGRIB2_BIN" \
        --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
        --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
        --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
        --no-local-cleanup --no-remote-cleanup \
        "${extra[@]}" || status=1
    fi
  done
fi
# Once daily, enforce 7-day retention (with protected-cycle exceptions) and
# refresh catalogs. The model lock prevents overlapping concurrent writers.
if [[ "$UTC_HOUR" == 03 && "$UTC_MINUTE" == 00 ]]; then
  echo '=== Daily model retention and catalog refresh ==='
  if [[ "${SOUNDING_PROFILES_ENABLED:-1}" == 1 ]]; then
    "$PYTHON_BIN" "$BASE_DIR/sounding_profiles.py" \
      --date "$(date -u +%Y%m%d)" --cycle 00 --models rrfs \
      --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
      --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
      --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
      --cleanup-only "${extra[@]}" || status=1
  fi
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$(date -u +%Y%m%d)" --cycle 00 --models all --cleanup-only --logs-dir "$BASE_DIR/logs" \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    "${extra[@]}" || status=1
  if [[ "$NO_PUBLISH" != 1 ]]; then
  "$PYTHON_BIN" "$BASE_DIR/multi_model_to_zarr_wcoss2.py" \
    --date "$(date -u +%Y%m%d)" --cycle 00 --models all --publish-only \
    --output-root "$OUTPUT_ROOT" --work-root "$WORK_ROOT" \
    --retention-days "$RETENTION_DAYS" --protected-cycles-file "$PROTECTED_CYCLES_FILE" \
    --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR" \
    --no-local-cleanup --no-remote-cleanup \
    "${extra[@]}" || status=1
  fi
fi
printf '=== incremental model poll complete, status=%s ===\n' "$status"
exit "$status"
