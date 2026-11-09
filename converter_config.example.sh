#!/usr/bin/env bash
# OMD EVU Converter v1.4.15 — shared cron and historical-case configuration.
# This is a starting template for converter_config.sh. Preserve all
# existing site-specific settings and private credentials when merging it.
# Worker values are concurrency caps, not strict CPU or memory quotas.
# The operational example below reflects a 256-logical-CPU / ~277-GiB host;
# monitor actual RSS and system load because GRIB decoding memory varies.

# Performance monitoring is intentionally OFF while the data flows stabilize.
# Normal processing/error cron logs remain enabled. To opt in later, set to 1.
# See PERFORMANCE_MONITORING.md for details and cleanup of legacy reports.
export EVU_PERF_ENABLED=0
export EVU_PERF_SAMPLE_SECONDS=5
export EVU_PERF_RETENTION_DAYS=7
# Optional override: export EVU_PERF_LOG_DIR=/path/to/performance/logs

# Scheduling and common retention/publication policy
export MODEL_SCHEDULER=incremental
export MODELS=all
# Legacy fallback: if a group-specific value is unset it uses RETENTION_DAYS.
export RETENTION_DAYS=7
export RRFS_RETENTION_DAYS=4      # RRFS, REFS, HRRR + their sounding profiles
export GFS_RETENTION_DAYS=7       # GFSv16/v17 + their sounding profiles
export OBS_RETENTION_DAYS=7       # RAP, MRMS, Stage IV, CCPA, NOHRSC, GLM, reports
export NO_PUBLISH=0
# Optional site-specific values can also be centralized here:
# export PUBLISH_HOST=user@web-server
# export PUBLISH_DIR=/path/to/web/data
# export PROTECTED_CYCLES_FILE=/path/to/protected_cycles.json
# export OUTPUT_ROOT=/path/to/zarr
# export WORK_ROOT=/path/to/work
# export PTYPE_OBS_SECRETS_FILE=/path/to/ptype_obs_secrets.sh

# Forecast-model cron
# Begin polling new operational RRFS/REFS cycles 15 minutes after initialization.
# Source-file availability still controls which forecast hours can be processed.
export INCREMENTAL_CYCLE_LAG_HOURS=0.25
export INCREMENTAL_CATCHUP_CYCLES=3
export INCREMENTAL_LOOKAHEAD=16
export INCREMENTAL_MAX_HOUR_JOBS=0  # Process every eligible missing hour; workers still cap concurrency
export INCREMENTAL_MISSING_RETRY_MINUTES=60
export INCREMENTAL_WORKERS=16
export INCREMENTAL_PARALLEL_STAGES=1

# GFS v16/v17 native 0.25-degree forecast conversion (optional separate cron).
# Set GFS_ENABLED=0 to disable without affecting RRFS/REFS/HRRR jobs.
export GFS_ENABLED=1
export GFS_MODELS=all
export GFS_LAG_HOURS=3.5
export GFS_CATCHUP_CYCLES=8
export GFS_WORKERS=2
export GFS_MAX_HOUR_JOBS=0  # Process every eligible missing lead per cycle/model
# Native-grid GFS sounding profiles are automatic by default.
export GFS_SKIP_PROFILES=0
export GFS_RETRY_MINUTES=45
# Optional local v17 root override: GFS_V17_BASE_DIR=/path/to/realtime
# The GFS cron uses GFS_RETENTION_DAYS, protected-cycle JSON, and
# publication destinations as the other forecast jobs. No analysis/obs ingest.

# Native sounding profiles in the incremental cron
export SOUNDING_PROFILES_ENABLED=1
export SOUNDING_PROFILE_MODELS=rrfs,hrrr
export SOUNDING_PROFILE_REFS_ENABLED=1
export SOUNDING_PROFILE_REFS_MEAN_ENABLED=1
export SOUNDING_PROFILE_RAP_ENABLED=1
export SOUNDING_PROFILE_MAX_HOURS=3
export SOUNDING_PROFILE_HOUR_WORKERS=4
export SOUNDING_PROFILE_GRID_STRIDE=4
export SOUNDING_PROFILE_RAP_STRIDE=1

# Native and aligned hourly analysis cron
export VERIFY_ENABLED=1
export VERIFY_LOOKBACK_HOURS=20
export MRMS_STRIDE=1
export ALIGNED_ENABLED=1
export ALIGNED_WORKERS=4
export ALIGNED_MAX_NEW_HOURS=16

# Observations and storm reports. Keep non-secret switches here so both cron
# wrappers use this one file. API tokens remain in ptype_obs_secrets.sh.
export PTYPE_OBS_ENABLED=0
export PTYPE_OBS_LOOKBACK_HOURS=6
export PTYPE_OBS_PUBLISH=0
export PTYPE_OBS_RIGHTS_CONFIRMED=0
export SPC_LSR_ENABLED=1
export SPC_LSR_LOOKBACK_DAYS=3
export HOURLY_PARALLEL_REPORTS=1

# GOES GLM lightning (netCDF4 is the only required NetCDF reader)
export GLM_ENABLED=1
export GLM_LOOKBACK_HOURS=3       # Recent completed hours revisited by hourly cron
export GLM_WORKERS=4              # Concurrent HTTP downloads (any positive integer); NetCDF parsing is serialized
export GLM_PUBLISH=1              # NO_PUBLISH=1 still overrides this
# GLM retention follows OBS_RETENTION_DAYS above; no separate GLM retention value.
export GLM_CASE_ENABLED=0         # Set to 1 to backfill GLM for historical create_case runs

# SCP/SSH publication; per-process transfer pool, not an extra CPU worker pool
export PUBLISH_WORKERS=4

# Legacy model schedule fallback if MODEL_SCHEDULER is changed
export WORKERS=16

# Historical create_case.py overrides. CLI options take precedence.
export CASE_WORKERS=18
export CASE_STAGE_WORKERS=4
export CASE_SOUNDING_WORKERS=4
export CASE_ALIGNED_WORKERS=4
export CASE_PUBLISH_WORKERS=4

# Resource guidance:
# - Case and cron do not overlap because of their coordinated file locks.
# - 20% of 256 logical CPUs is about 51 logical CPUs; this is guidance, not a quota.
# - 20% of ~277 GiB available memory is about 55 GiB; peak decode memory varies.
# - Reduce CASE_SOUNDING_WORKERS or CASE_STAGE_WORKERS if RSS approaches the
#   desired memory budget. The GLM workers primarily perform network I/O.
# - Conservative cron: INCREMENTAL_WORKERS=12, SOUNDING_PROFILE_MAX_HOURS=2,
#   SOUNDING_PROFILE_HOUR_WORKERS=2, ALIGNED_WORKERS=2, PUBLISH_WORKERS=3.
# - Conservative case: CASE_WORKERS=12, CASE_STAGE_WORKERS=2,
#   CASE_SOUNDING_WORKERS=2, CASE_ALIGNED_WORKERS=2, CASE_PUBLISH_WORKERS=3.

# Synoptic/mPING API credentials remain in the separate, mode-0600
# ptype_obs_secrets.sh file. Put non-secret PTYPE_OBS_* switches in this file.

# AIGFS / AIGEFS mean / HGEFS mean / GEFS mean (independent 30-minute poll)
# Forecasts are 6-hourly; HGEFS ends F240, the other three up to F384.
# Start at 4h lag and tune from real NOMADS/AWS inventories and logs.
export AI_ENABLED=1
export AI_MODELS=all
export AI_LAG_HOURS=4.0
export AI_CATCHUP_CYCLES=3
# The three operational AI models use the shared local filesystem by default.
# GEFS continues to use AWS. These settings do NOT alter the other pollers.
export AI_SOURCE_MODE=local
export AI_LOCAL_WORKERS=8
export AI_REMOTE_WORKERS=3
export AI_NOMADS_FALLBACK=0
# Optional local source root overrides (normally unnecessary):
# export AI_AIGFS_LOCAL_BASE=/lfs/h1/ops/prod/com/aigfs/v1.1
# export AI_AIGEFS_LOCAL_BASE=/lfs/h1/ops/prod/com/aigefs/v1.0
# export AI_HGEFS_LOCAL_BASE=/lfs/h1/ops/prod/com/hgefs/v1.0
export AI_MAX_HOUR_JOBS=12
export AI_RETRY_MINUTES=45
export AI_RETENTION_DAYS=7
# Optional remote endpoint overrides: AI_AIGFS_BASE, AI_AIGEFS_BASE, AI_HGEFS_BASE, AI_GEFS_BASE
