# Performance monitoring coverage — v1.4.42

Monitoring remains **opt-in**, controlled by `EVU_PERF_ENABLED=1` in the existing shared configuration. This release adds the previously missing whole-poll AI monitor and AI/GFS task-level timings, while preserving the established model, observation, sounding, case, and publication reports. It does not alter the number of forecast workers, process concurrency, forecast fields, protected cycles or output files.

Every completed monitored invocation creates up to four files under `logs/performance/`: `_samples.jsonl` (CPU, memory and disk I/O over time), `_events.jsonl` (stage/hour/task/transfer events), `_summary.json` and `_summary.txt`. Ordinary cron/error logs under `logs/` are separate and unaffected. A skipped cron run, incomplete run, inventory-only check, disabled monitoring, or model with no eligible data might have no full task records. The AI/GFS child script summaries contain detailed hour stages, while the cron-level reports cover the combined process tree. **Do not add these separate duration, CPU or byte totals together**, because both can observe the same work.

The GFS and AI sub-stages include local/remote inventory/acquisition, GRIB crop/IEEE decode, per-hour processing, Zarr commit, GFS sounding and marine processing, difference generation and per-store delta publication. Existing regional/verification task logs remain enabled when opted in. Download, compression and Zarr-write counters are thread-local to prevent attribution to concurrent workers; nested sub-stage timings are intentionally timing-only so byte counters are not double counted.

To see which workflows have generated complete reports recently:

```bash
python3 summarize_performance_logs.py --hours 24
python3 summarize_performance_logs.py --hours 72 --log-dir /path/to/logs/performance
```

The report is read-only and can show `no completed report in this window` for workflows that have not executed. Treat it as a visibility tool, not a failure monitor. Reports are pruned by the existing retention policy; the monitor requires Python standard library and Linux `/proc` only. Keep the sampling interval >= 5 seconds on busy shared systems unless diagnosing a short-lived problem.

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

The shipped `converter_config.example.sh` includes these settings but keeps monitoring disabled by default until explicitly enabled. Monitoring requires only the Python standard library and Linux `/proc`; it does not require `psutil`, `h5py`, or any package installation. GLM continues to use the already-installed `netCDF4` package.

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

All four cron wrappers (hourly verification, incremental regional models, GFS, and AI) create whole-cron process-tree reports after acquiring their processing locks. Individual Python entry points also produce focused reports during normal cron use. A historical `create_case.py --execute` run produces one parent case report covering its subprocess tree, plus detailed stage/task events from child workflows. Nested child tools contribute events to the parent case report instead of creating duplicate process-tree monitors.

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

The GFS cron launches the `gfs_incremental_cron` whole-poll process sampler. The separate `gfs_conversion` report records per-hour processing, source acquisition, decoding, compression/Zarr commits, soundings, marine products and differences where those tasks ran. The AI cron similarly records `ai_incremental_cron` and `ai_conversion`, including all AI/GEFS member-mean models and both local/remote source paths. Do not sum child and whole-poll resource totals. Use `summarize_performance_logs.py` to see which workflows have complete reports.
