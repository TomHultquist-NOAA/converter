# Performance monitoring — paused by default

**Current policy (v1.4.39): Performance sampling is disabled by default.**
Ordinary processing, error, acquisition and publication logs remain enabled.
Existing reports from the earlier architecture can be removed safely without
removing any model data or the operating logs:

```bash
python3 cleanup_performance_logs.py                  # preview only
python3 cleanup_performance_logs.py --apply           # delete only old performance reports
```

If the live `converter_config.sh` still has `export EVU_PERF_ENABLED=1`,
change it to `export EVU_PERF_ENABLED=0` before the next cron invocation.
For a custom telemetry directory, add `--log-dir /path/to/performance`.
Avoid cleaning an active monitor; stop its invocation first.

Performance instrumentation is preserved for future opt-in benchmarking:
`EVU_PERF_ENABLED=1` re-enables it, using the existing sample interval and
retention controls. This does not affect the model and verification worker
limits, protected cases, or ordinary operational logs.

---

# OMD EVU Converter Performance Monitoring

## Purpose

The performance monitor provides measurements for tuning throughput while retaining the existing worker, lock, retry, recovery, and publication policies. It is observational: it does not throttle jobs, increase worker counts, change compression levels, or enforce CPU/memory quotas.

## Configuration

The shared `converter_config.sh` can contain:

```bash
export EVU_PERF_ENABLED=1
export EVU_PERF_SAMPLE_SECONDS=5
export EVU_PERF_RETENTION_DAYS=7
# Optional: export EVU_PERF_LOG_DIR=/path/to/performance/logs
```

The shipped `converter_config.example.sh` includes these defaults. Monitoring requires only the Python standard library and Linux `/proc`; it does not require `psutil`, `h5py`, or any package installation. GLM continues to use the already-installed `netCDF4` package.

- `EVU_PERF_ENABLED=1`: opt in to monitoring (default is disabled).
- `EVU_PERF_ENABLED=0`: disable monitoring.
- `EVU_PERF_SAMPLE_SECONDS=5`: process-tree sample interval; valid values are bounded internally to 1–60 seconds. A shorter interval can capture brief peaks but increases sampling overhead slightly.
- `EVU_PERF_RETENTION_DAYS=7`: retention for performance report files, bounded by the configured data retention period during scheduled cleanup. This does not independently change model, analysis, observation, GLM, or log retention.
- `EVU_PERF_LOG_DIR`: optional common report directory. By default, cron and case workflows use `BASE_DIR/logs/performance/`.

## Files produced

Each run creates four files with a shared `evu_perf_...` run identifier:

- `*_samples.jsonl`: process-tree resource samples at the configured interval.
- `*_events.jsonl`: stage, task, queue, run-configuration, and transfer events.
- `*_summary.json`: machine-readable aggregate results.
- `*_summary.txt`: concise human-readable report.

All three cron wrappers (hourly verification, 15-minute model incremental, and 30-minute GFS incremental) create whole-cron process-tree reports after acquiring their processing locks. Individual Python entry points also produce focused reports during normal cron use. A historical `create_case.py --execute` run produces one parent case report covering its subprocess tree, plus detailed stage/task events from child workflows. Nested child tools contribute events to the parent case report instead of creating duplicate process-tree monitors.

## Measurements

### Process-tree samples

- `cpu_cores`: average CPU core-equivalents used during the sample interval. For example, 8.0 means approximately eight logical CPUs were busy with this process tree.
- `cpu_host_pct`: process-tree CPU as a percentage of the host's total logical CPUs. On a 256-logical-CPU host, 8 cores is about 3.1% of the host.
- `rss_gib`: sum of resident memory across the process tree.
- `process_count`: number of visible processes in the process tree.
- `read_mb_s` / `write_mb_s`: process-attributed disk I/O rates from Linux `/proc` counters.
- `host_mem_available_gib` and `host_load1`: host context, not attribution to the converter alone.
- `effective_cpu_capacity`, `cpu_capacity_pct`, and optional cgroup quota/cpuset fields: show the CPU capacity visible to the process, including scheduler/container restrictions when Linux exposes them.
- Optional cgroup memory limit/available fields: provide the job/container memory budget when exposed by the host; host-wide `MemAvailable` remains available for context.

Summed RSS can count shared pages more than once. Process-tree CPU and I/O sampling can miss some final counters from very short-lived children that start and exit between samples. Use these as operational measurements, not accounting-grade totals.

### Stage and task events

The event summary reports count, total/mean/maximum elapsed time, failures, and maximum observed concurrency by category. Important categories include forecast hours, verification ingestion, analysis Zarr conversion, aligned analysis stores, sounding-profile hours, GLM hours, and case stages. Case reports also include stage queue waits; forecast-hour tasks record time waiting between submission and worker start.

### Compression and downloads

Per-task metrics include Zlib compression time and input/output bytes, Zarr chunk write time, GRIB index/range-download bytes and time, verification analysis download bytes and time, GLM download bytes and time, and serialized GLM NetCDF parse time. These counters help distinguish CPU-bound compression from slow downloads or storage writes.

### Publication

SSH and SCP commands are timed individually. For SCP, the local source-file size is recorded, allowing aggregate uploaded MiB and average SCP MiB/s to be estimated. Concurrent SSH and SCP operations are measured from transfer start/end events, so the report can confirm whether configured publication parallelism is actually being used. SSH command payload size is not guessed. Command strings, credentials, and authentication material are never written to performance events.

## Recommended baseline procedure

1. Install the complete converter release and merge the three `EVU_PERF_*` settings into the existing shared configuration. Do not replace site-specific settings.
2. Keep all existing worker settings unchanged for the first baseline.
3. Allow at least 24 hours of ordinary cron operation so reports include different models, forecast hours, sounding work, analysis arrivals, and publication conditions.
4. Run one representative historical case with the usual models, analyses, soundings, observations, and GLM settings. Use a case already suitable for operational testing; do not force-reprocess data solely to benchmark it.
5. Compare whole-cron and focused reports. Identify the largest elapsed stages, low versus high CPU core use, peak RSS, disk I/O rates, task concurrency, queue waits, download throughput, and SCP throughput.
6. Change one concurrency or processing parameter at a time. Repeat a comparable workload and compare elapsed time and peak resource use before retaining the change.

The preferred CPU/memory target remains a planning guideline, not a hard limit. On a 256-logical-CPU / approximately 277-GiB host, 20% is about 51 logical CPUs and 55 GiB. The reports measure the converter process tree, while host memory availability and load provide context. Keep a safety margin for other system work and variable GRIB decode memory.

## First review

For the first review, the most useful files are the `*_summary.txt` and `*_summary.json` files from both cron wrappers, plus the case summary and its `*_events.jsonl`. The detailed sample JSONL is useful when a peak or throughput interval needs closer inspection. Do not change worker settings until a representative baseline has been collected.

## GFS monitoring coverage

The GFS cron launches `perf_monitor.py --watch-pid` with the `gfs_incremental_cron` label and the same `EVU_PERF_*` settings used by the other cron jobs. Reports are located under `logs/performance/` by default. The GFS sampler captures process-tree CPU, memory, disk I/O, and elapsed time, including converter subprocesses while they are running. GFS-specific per-hour event timings are not yet instrumented inside `gfs_to_zarr.py`; avoid interpreting an absence of per-hour GFS events as an absence of processing.
