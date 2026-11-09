# OMD EVU — Case-study creation and preservation

The `create_case.py` utility coordinates forecast-model conversion, full native sounding profiles (forecast and hourly RAP analysis), native verification analyses, aligned analysis fields, Synoptic METAR/mPING precipitation-type reports, and optional GLM lightning observations over the UTC valid-time span of one or more model cycles. It uses the **existing** converter, Zarr v2 output format, SSH/SCP publication, and operational retention policy. Model, verification, sounding and aligned-analysis transfers honor the same configurable `--publish-workers` setting; observation/report publication uses a reused SSH session and single remote-directory setup per batch. GLM collection uses the existing `netCDF4` package; `h5py` is not required. GLM granule downloads may run concurrently, but NetCDF4 parsing is serialized in each collector process because the underlying netCDF-C library is not thread-safe.

## Performance reports

When `EVU_PERF_ENABLED=1` (opt-in; disabled by default beginning with v1.4.39), an executed case writes a process-tree performance report to the shared `logs/performance/` directory. The report includes total case CPU/memory/disk-I/O sampling, stage elapsed times and queue waits, per-hour forecast/analysis/sounding task timings, and measured publication commands. The report is observational only and does not change case worker settings. See `PERFORMANCE_MONITORING.md` for field definitions and how to use the reports when tuning worker counts.

## Targeted RRFS/REFS 0–1 km shear repair (v1.4.8)

For an existing protected case whose RRFS/REFS native low-level shear is suspect, run a narrow repair that preserves all other model fields, analyses, profiles, and observations. The normal exclusive case locks are acquired automatically:

```bash
python3 create_case.py --cycle 2026102812 --lowlevel-shear-only
python3 create_case.py --cycle 2026102812 --lowlevel-shear-only --execute
```

The repair uses native 3-km surface pressure, 10-m winds, pressure-level winds, and height records already in Zarr when sufficient; otherwise it selectively downloads missing inputs. It replaces only `shru01`, `shrv01`, and `shear01` for RRFS and REFS members. Affected cells with insufficient above-ground profile information remain missing, never zero. The separate REFS mean and all 0–6 km shear fields are unchanged. Test the Minneapolis 2026-10-28 22 UTC F010 location and inspect the updated audit before considering the change scientifically verified.

## Automatic case/cron coordination (v1.4.2)

The case execution command now waits for both currently running cron jobs to exit before starting work; new scheduled cron launches are skipped while the case is waiting or running. After case completion, cron resumes automatically on its next scheduled invocation. Keep both cron entries enabled. The same behavior applies to the standard missing-data and forced-reprocessing modes. Install the updated `create_case.py`, both cron wrappers and `case_cron_coordination.sh` together. No new environment variable is required. Do not manually delete the `.case_*` or legacy cron lock files; flock locks are automatically released.

## Shared worker settings (v1.4.3)

`create_case.py` reads `converter_config.sh` automatically. A specific file can be selected with `--config-file` or `CONVERTER_CONFIG_FILE`; the old `INCREMENTAL_CONFIG_FILE` environment variable remains supported as a deprecated alias. CLI values take precedence. The same file supplies case worker limits, retention, `NO_PUBLISH`, model selection, output/work roots, publication host/directory, protected-cycle path, observation-secrets path, and optional GLM case settings. Non-secret Synoptic/mPING switches are also kept in that file; API tokens remain in the separate mode-0600 secrets file. The included `converter_config.example.sh` is a complete suggested configuration; install it as a local `converter_config.sh` only after preserving any site-specific settings. This file also controls both installed cron wrappers.

| Case flag | Case-specific variable | Cron-shared fallback | Built-in case default |
|---|---|---|---:|
| `--workers` | `CASE_WORKERS` | `INCREMENTAL_WORKERS` then `WORKERS` | 18 |
| `--stage-workers` | `CASE_STAGE_WORKERS` | None (cron parallelism is a switch) | 3 |
| `--sounding-workers` | `CASE_SOUNDING_WORKERS` | `SOUNDING_PROFILE_HOUR_WORKERS` | 3 |
| `--aligned-workers` | `CASE_ALIGNED_WORKERS` | `ALIGNED_WORKERS` | 3 |
| `--publish-workers` | `CASE_PUBLISH_WORKERS` | `PUBLISH_WORKERS` | 4 |

The case prints effective worker values and sources before processing; verify with a dry run. Its native subprocesses run with one OpenMP/BLAS thread each, as in cron. On a 256-logical-CPU system, 20% represents about 51 logical CPUs, but the worker caps are **not hard quotas**. The amount of physical memory used by concurrent native GRIB decoding varies, so monitor RSS, swap, and CPU contention during the first run and reduce sounding/stage concurrency if memory approaches the desired budget. Case/cron execution locks still prevent a manual case from overlapping cron processing.

## Stage scheduling and resource limits (v1.4.0)

The built-in default **`--stage-workers 3`** runs at most three independent case-processing
stages at once. This overlaps source downloads, conversion and publication
where safe; it is not a promise that every individual forecast hour starts as
soon as its input appears. Dependencies are tracked at the subprocess/batch
level. The forecast-model catalog has one writer across case cycles; all
forecast/RAP sounding profile updates share one serial catalog chain. Native
verification for a valid-time batch precedes alignment of that batch, and
aligned-analysis catalog updates are serialized. Independent observation and
SPC batches are allowed to proceed while model work continues. On failure,
work requiring the failed stage is skipped, and unrelated stages continue;
case creation exits nonzero and identifies skipped stages.

Concurrent subprocess output is stored separately under
`work/case_stage_logs/<initial cycle>_<UTC timestamp>/*.log` (private
mode-0600 files), avoiding unreadable interleaved logs. The command emits
start/finish summaries and the log directory path. Set `--stage-workers 1`
for the previous sequential behavior and live streaming output, or use 2–4
only if server capacity permits. The independent built-in caps `--workers 18`,
`--aligned-workers 3`, `--publish-workers 4`, and `--sounding-workers 3` are still in force **within**
each active stage. Concurrent stages can therefore consume more resources in
aggregate than the previous serial workflow. The `--sounding-workers N` parameter (1–4, built-in default 3) controls concurrent
independent native sounding-hour GRIB downloads and conversions **within** a
profile stage. The profile catalog remains single-writer. Forecast profiles
are reconciled after the model conversion stage, filling hours unavailable
at the start of the first profile pass. Three sounding workers is the packaged starting
recommendation; higher values require careful CPU, memory, disk and network
checks. No new scheduler service, permissions, third-party packages or cron
entry is needed.

## Quick start

Run from the installed processing server processing directory with the operational Python/wgrib2 environment loaded. Active cron jobs finish before the case starts; new cron launches then skip automatically. These commands **preview** the case without writing anything:

```bash
python3 create_case.py --cycle 2026102900
python3 create_case.py --start-cycle 2026102800 --end-cycle 2026102918
```

Create and publish **one case**, retrieving only missing or incomplete data:

```bash
python3 create_case.py --cycle 2026102900 --execute --rights-confirmed
```

Create and publish a **range of cycles**, inclusive and in six-hour initialization steps:

```bash
python3 create_case.py \
  --start-cycle 2026102800 --end-cycle 2026102918 \
  --execute --rights-confirmed
```

For a local-only test, use `--execute --no-publish` (rights confirmation is needed only when publishing Synoptic/mPING reports). To explicitly reacquire and overwrite all existing model hours and analysis products, specify `--mode all`; the safer default, `--mode missing`, preserves already populated fields and uses the manual case-specific missing-field retry option to check all configured forecast hours without waiting for the scheduled retry cooldown. `--mode all` can replace valid existing data with missing fields if original source files are no longer available, so backups are recommended.

### Complete case refresh with improved sounding moisture

To recover `2026102812` in a single step, **including all available forecast and hourly RAP soundings** and rebuilding previously written profile hours that may be missing dewpoints from an older release:

```bash
python3 create_case.py --cycle 2026102812 --execute \
  --mode missing --reprocess-analysis --reprocess-soundings --rights-confirmed
```

This leaves existing valid forecast/analysis map fields intact. `--reprocess-soundings` specifically replaces **every already completed sounding-profile hour** using source-native fields; this can take a long time over a large case and should be omitted during subsequent missing-only resumes; `--reprocess-analysis` forces native verification fields (including prior RAP shear) and allows aligned-analysis stores to refresh. If observation credentials/authorized publication are not configured, replace `--rights-confirmed` with `--skip-observations`. Without `--reprocess-soundings`, already completed sounding hours are preserved and only missing hours are generated. `--mode all` also rebuilds soundings, but it **overwrites model/analysis map fields**, so reserve it for deliberate full recovery. All operations retain the existing protected-case status. Use the normal dry-run without `--execute` to preview the scope.

## Required configuration

- The existing operational `protected_cycles.json` must already exist, be readable, and pass `retention_policy.load_policy`. The file is **never** included in the package and is never replaced with an example during installation.
- The installed converter's Python 3.10+ runtime and `wgrib2` must already work on processing server. If needed, pass `--wgrib2 /absolute/path/to/wgrib2` and/or `--python /absolute/path/to/python3`.
- Observation retrieval requires a private `ptype_obs_secrets.sh` alongside the scripts, containing `SYNOPTIC_API_TOKEN` and `MPING_API_KEY`, with file mode `600`. No credentials are logged or distributed. `--skip-observations` runs only forecasts and analyses if observation credentials are not configured.
- Synoptic/mPING publication requires `--rights-confirmed`, reflecting the data-provider publication permissions. Without this flag, a publishing execution is refused before modifying the retention file.
- The case command waits for active cron jobs while preventing new scheduled launches from starting, provided the crontab invokes the installed coordinated wrappers and both jobs use the same working filesystem with functioning distributed `flock`. The case logs the canonical lock directory and checks it against both installed cron `BASE_DIR` settings. A mismatch aborts before processing; in custom installations the `--lock-dir` option selects the cron lock directory explicitly. Do not delete lock files or replace scripts during an active run.

## Case/cron lock troubleshooting

**Package correction in v1.4.7:** The prior v1.4.6 archive incorrectly contained hidden, empty lock files. Overwriting a live lock file with `unzip -o` can replace its inode while a cron process still owns a lock on the original inode; the next case then sees an unlocked new file. The current release excludes all lock files, and its archive builder validates this. Existing locks must remain untouched while jobs are active.

The case tool and both cron wrappers must acquire the **same lock files in the same directory**. An apparent overlap can occur if the case script is executed from a separately unpacked release, if a cron entry calls a different wrapper or launches the converter directly, or if different machines do not share working `flock` semantics. A cron process may also be in its startup phase before acquiring the gate; it should subsequently skip when a case owns the gate.

For a read-only check, compare `grep '^BASE_DIR=' run_model_incremental_cron.sh run_multimodel_cron.sh` with the case startup log's `Case/cron lock directory:` line. Inspect `crontab -l` and `ps -ef | grep -E '[r]un_model_incremental_cron|[r]un_multimodel_cron|[c]reate_case.py'` to confirm which scripts are actually running. Do not manually delete `.lock` files while operations are running. Allow existing work to finish before installing a newer release; recheck catalogs if concurrent writers actually targeted the same stores.

## Data and time selection

Forecast cycles must be valid UTC initialization hours (`00`, `06`, `12`, or `18`). One-cycle selection uses `--cycle YYYYMMDDHH`; a range uses `--start-cycle` and `--end-cycle` in the same format. Ranges are inclusive and support up to 124 cycles per invocation; small batches (one to four cycles) are preferable for initial validation and operational load control.

By default, all eight model datasets are selected: RRFS, HRRR, REFS Mean, and REFS m001–m005. Set `--models rrfs,hrrr` (or another comma-separated subset) to restrict models. Individual models retain their native maximum forecast-hour limits (RRFS F084, REFS Mean/members F060, HRRR F048; REFS Mean starts at F001). The case command requests **all available processed hours** for selected forecast-model native sounding profiles rather than the four-hour limit used in the standalone profile utility. RAP profiles are processed for each available hourly analysis valid time, in batches of up to 168 hours. Vertical resolution still depends on source-native GRIB fields; the converter does not interpolate absent humidity levels.

The case verification window extends from the earliest selected initialization time through **F084 of the latest selected initialization**. It covers all shorter-range models and permits full comparisons, not merely the time of initialization. Native verification processing includes RAP, RTMA/URMA, MRMS, and scheduled NOHRSC products with their existing data-latency and field-availability rules. The alignment pass regrids available verification fields onto the forecast grid. Historical verification uses exact UTC `--start-hour` / `--end-hour` windows, split into at most 168 consecutive hours per command. Source datasets unavailable at their original archives cannot be fabricated.

Synoptic/mPING collection uses the same valid-time coverage, divided into at most 168-hour API windows. It writes one sanitized hourly observation GeoJSON file per valid hour with the existing ±30-minute matching window, then publishes it when requested and authorized. Observation hours whose ±30-minute window is not yet complete are deferred. The script must be rerun later for future valid times, delayed reports, or verification fields not yet available. Repeating the same `--execute` invocation is safe in `--mode missing` and updates the same protected case.

When `GLM_ENABLED=1` **and** `GLM_CASE_ENABLED=1` are set in the shared `converter_config.sh`, the case utility also collects GLM flash-centroid GeoJSON for the exact available case valid-hour range, in sequential batches of at most 168 hours. Existing complete local hours are reused; missing hours are retried, and publication follows `GLM_PUBLISH` plus the global `--no-publish` override. GLM backfill is separate from Synoptic/mPING (`--skip-observations` does not disable it); use `--skip-glm` to omit it for one case. GLM is deliberately non-fatal to the primary case workflow: incomplete source hours, download errors, and GLM publication failures are logged as warnings, later GLM batches still run, and the case can finish successfully if its forecast, analysis, sounding, and other requested stages succeed. The collector writes hourly GeoJSON atomically, so an unavailable or incomplete hour has no valid cached file and is retried on the next execution of that case; already complete local hours are reused and can be republished. Because the source contains about 180 twenty-second granules per satellite per hour, historical backfill can involve substantial network traffic. `GLM_CASE_ENABLED` therefore defaults to 0 even when routine `GLM_ENABLED` cron collection is on. The collector uses the existing `netCDF4` package only; no `h5py`, API token, or extra cron entry is required.

## Protected-cycle updates

With `--execute`, the utility validates `protected_cycles.json`, takes both operational locks, then **atomically merges** selected cycles into `protected_cycles`. It retains previously protected cycles and every `protected_analysis` entry, and merges model subsets without reducing any existing protection. Each updated operational list has a timestamped mode-0600 backup alongside it (`protected_cycles.json.case-backup-*`). The list remains mode 0600 and is excluded from Git. This protection is written **before** downloads/publishing so interrupted case studies remain safe from retention cleanup.

The existing retention policy automatically protects selected model stores and matching verification/analysis stores from initialization through F084, including when an older case falls outside the normal 7-day window. Hourly Synoptic/mPING and GLM GeoJSON files use the same configured `RETENTION_DAYS` window; valid hours covered by protected cases are retained beyond that window. SPC report-day files are retained when their 12Z–12Z reporting window intersects a protected case. Stale interrupted observation uploads are cleaned by age, while recent temporary files and symlinks are preserved. Protection cannot restore source data that had already been deleted before the cycle was selected.

## Publishing, recovery and status

The utility uses non-overwriting model conversion by default (`--incremental --force-missing-check`), then runs a `--publish-only` step for **every successfully converted selected cycle** so complete stores and catalogs are published. Other independent stages can run concurrently. Native verification must succeed for a batch before that batch is aligned, and forecast/RAP sounding catalog updates are serialized. The sounding-profile catalog is published after successful profile-store transfers. `--no-publish` affects all three publication paths. Every stage retains its command and output (without printing provider credentials), and failures are reported in the final error summary. Dependent stages are skipped on failure; unrelated work continues.

The tool exits nonzero if any attempted step reports a failure, including sounding creation/publication failures. It may still exit zero while some sources/forecast hours are legitimately not yet available, so the model catalogs, analysis catalogs, and hourly observation file counts should be checked for case completeness. Re-running the same case is the supported recovery mechanism. Do not edit model Zarr stores or delete `.published/` snapshots to trigger recovery; `--mode all` is unnecessary for normal retries.

### Optional flags

| Flag | Behavior |
|---|---|
| `--config-file PATH` | Override the auto-loaded trusted shared cron/case worker configuration file |
| `--mode missing` | Default; process missing forecast hours/fields, leaving existing chunks intact |
| `--mode all` | Explicitly overwrite existing forecast/analysis data when source files remain available |
| `--no-publish` | Retain output locally; no remote SCP/SSH publication |
| `--skip-analysis` | Omit native/aligned verification and RAP analysis soundings |
| `--skip-soundings` | Disable both forecast and RAP sounding creation/publication |
| `--reprocess-soundings` | Rebuild already complete sounding hours even with `--mode missing` |
| `--reprocess-analysis` | Recompute native verification fields (e.g. old RAP shear) and refresh aligned analyses with `--mode missing` |
| `--publish-workers N` | Parallel SSH/SCP store transfers, 1–6 (built-in default 4; config can override) |
| `--stage-workers N` | Concurrent independent case stages, 1–4 (built-in default 3); 1 restores sequential execution |
| `--sounding-workers N` | Parallel sounding-hour GRIB decodes within each sounding stage, 1–4 (built-in default 3) |
| `--skip-observations` | Omit Synoptic and mPING collection |
| `--skip-glm` | Omit GLM lightning case backfill even when `GLM_CASE_ENABLED=1` |
| `--workers N` | Number of concurrent forecast-hour workers, 1–20 (built-in default 18) |
| `--aligned-workers N` | Independent regridding processes, 1–4 (built-in default 3) |
| `--models ...` | Restrict case-study model IDs; retained models are merged into existing protection |
| `--rights-confirmed` | Required when executing with authorized Synoptic/mPING publication |

The case utility does not modify either cron entry, the viewer, published file formats, or existing operational paths. It adds the optional case-scoped GLM stage only when both GLM configuration switches are enabled.

## v1.4.5 publication and sounding recovery

Historical case `--mode missing` now reconciles the native analysis remote catalog against the actual remote store inventory before publishing. Configured `RETENTION_DAYS`/`CASE_RETENTION_DAYS` is honored; expired, unprotected local historical datasets are retained locally but excluded from the remote catalog. Existing protected analysis hours remain included. Missing remote protected stores are reuploaded from the local completed Zarr store when available. Native sounding `sp`, `t2m`, `td2m` fields are also checked and repaired without forcing expensive pressure-level reprocessing.

## Native low-level shear, alternate RAP products (v1.4.6)

RAP F000 analyses first use the existing `awp130` source. For native 0–1 km shear and storm-motion components missing there, the ingestion utility accesses indexed native `wrfprs` GRIB records, downloads only the necessary byte ranges, and regrids to the established analysis grid with an exact geographic-geometry check. Native fields are preferred over a conservatively derived fallback. Do not append records from incompatible source grids directly. Missing REFS member `USTM`/`VSTM` cannot be recovered by repeating a missing-data pass when neither member product supplies them. For LL shear direction comparisons, inspect `shear01_orientation` printed by `audit_storm_fields.py`.

## Saving a GFS v16/v17 case

The GFS case utility is separate from the RRFS/REFS case utility. The protected
cycle configuration is shared; a GFS-only case does **not** select RRFS/REFS
forecast members. The GFS case protects both selected model Zarr stores and the
shared verifying observation/analysis time range through F240. The existing
RRFS/REFS case window remains F084. Shared hourly Synoptic/mPING data, SPC
reports, and available GLM lightning are protected; the utility can backfill
those observations using the existing collectors and publication location.

Run from the installed converter directory. An operational `protected_cycles.json`
file must already exist alongside the converter, as is required for incremental
operation. Its entries are backed up before being changed.

```bash
python3 create_gfs_case.py --date 20261028 --cycle 12
python3 create_gfs_case.py --date 20261028 --cycle 12 --execute
```

The first command **only prints a plan**. The second protects the cycle, runs
GFS conversion and soundings, and backfills the shared observation products.
Use `--models gfs_v16` or `--models gfs_v17` to retain one model; by default
both are selected. `--no-publish` runs locally. Options `--skip-observations`,
`--skip-spc`, and `--skip-lightning` disable those backfills without shortening
the protection interval. To collect Synoptic/mPING, provide the operational
`ptype_obs_secrets.sh` credentials file with mode 0600 (or `--secrets-file`).
The script shares the case-vs-cron locks with the existing workflow.

**Important:** Creating a case protects data against normal cleanup but cannot
recover observation hours already unavailable from upstream data providers.
Any failed backfill is reported with a nonzero exit status; the protected case
entry is retained so it can be repaired safely.
