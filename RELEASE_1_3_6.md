# OMD EVU Converter v1.3.6 — Bounded throughput tuning

Released: October 30, 2026

## Purpose

Increase forecast-hour and analysis-alignment throughput through conservative, configurable worker defaults, while preserving bounded resource use and existing data products. Worker counts are not hard CPU- or memory-usage quotas.

## Changes

- Legacy hourly/six-hourly model cron default: `WORKERS=16` (previously 8).
- Incremental model watcher default: `INCREMENTAL_WORKERS=12` (previously 6).
- Case-study model default: `--workers 12` (previously 4); the supported explicit range becomes 1–20 (same as the main model converter).
- Native-to-forecast-grid **aligned analysis** can process separate valid-hour stores in parallel using `aligned_analysis.py --workers N` (1–4). The default remains 1 for direct invocations; main cron and case utility now pass **2** unless overridden with `ALIGNED_WORKERS` or `--aligned-workers` respectively. Worker-local projection-map caches avoid cross-process mutable state.
- Model and alignment jobs remain bounded. `wgrib2` and BLAS/OpenMP continue to run with **one thread per subprocess** to avoid processing server OpenMP thread exhaustion. Existing locks, catalog publication order, seven-day retention, protected cases, and all Zarr formats are unchanged.
- Existing **private** `incremental_model_config.sh`, if present, overrides these cron defaults. The supplied **example** now shows the recommended throughput settings; do not overwrite an active private configuration file while deploying.

## Suggested safe operating settings

For a processing server with sufficient allocated resources, start with `WORKERS=16`, `INCREMENTAL_WORKERS=12`, and `ALIGNED_WORKERS=2`. Concurrent workflows can run multiple conversion and alignment worker processes; these counts do not guarantee a particular CPU percentage, and staging, publishing, and profile tasks also consume resources. During an isolated case backfill, use `create_case.py --workers 12 --aligned-workers 2` (the new defaults). Leave cron disabled until the case finishes.

If the shared filesystem, network, or CPU utilization becomes high, lower settings to `WORKERS=10`, `INCREMENTAL_WORKERS=8`, `ALIGNED_WORKERS=1`, and for cases `--workers 8 --aligned-workers 1`. Avoid adjusting `OMP_NUM_THREADS` upward. Measure actual throughput and iowait using `top`, `ps`, and converter logs; `nproc` may report logical CPUs visible to the host rather than the job allocation. Respect site quotas and policies. There is no guarantee of speedup for network/GRIB-source-bound workloads.

## Compatibility and limits

No additional Python packages, new cron entries, data migrations, or case reprocessing are required just for this performance update. The RAP field-inventory repairs from v1.3.5 and all audit utilities remain included. Native verification staging and web server transfers are still mostly sequential, so they can remain the dominant time costs for long case-study runs. Parallel alignment is capped at 4 because memory and storage access per store can be substantial, particularly in stdlib-only mode without NumPy.

## Verification

Run `python3 -m unittest discover -s tests`. Syntax-check both cron scripts using `bash -n`. Confirm effective worker values in each cron log. Validate with a normal processing cycle and check host memory, CPU and I/O before increasing any limits.
