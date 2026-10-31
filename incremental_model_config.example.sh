#!/usr/bin/env bash
# Copy to incremental_model_config.sh in the WCOSS2 processing directory.
# This file is read by BOTH the new model watcher and existing hourly wrapper.
export MODEL_SCHEDULER=incremental
export INCREMENTAL_CYCLE_LAG_HOURS=0.75
export INCREMENTAL_CATCHUP_CYCLES=3
export INCREMENTAL_LOOKAHEAD=12
export INCREMENTAL_MAX_HOUR_JOBS=48
export INCREMENTAL_MISSING_RETRY_MINUTES=60
export INCREMENTAL_WORKERS=12
# Shared wrapper uses WORKERS for legacy scheduled model runs.
export WORKERS=16
# Analysis alignment is deliberately limited to 2 workers to protect I/O.
export ALIGNED_WORKERS=2
