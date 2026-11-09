> **Historical reference:** As of converter v1.4.39, performance monitoring is OFF by default; use `EVU_PERF_ENABLED=1` only when intentionally re-enabling it. The following older sections describe earlier behavior and should not be interpreted as current defaults.

# Archived technical reference (not the authoritative release guide)

This long-form reference was written across older release versions and may
contain obsolete version numbers and environment-specific instructions.
Use README.md, ARCHITECTURE_AND_AUDIT.md and the latest release notes as the
authoritative 1.4.35 entry point. The original content is retained below
so valuable diagnostics are not discarded.

---

## Latest release

Converter v1.4.34 accepts any positive worker count for case and component workflows, without previous artificial concurrency ceilings. Existing worker defaults and protected-cycle/lock handling are unchanged. See RELEASE_1_4_34.md. The v1.4.33 GFS-aligned MRMS precipitation-rate analysis is retained.

**Current release: v1.4.31.** GFS precipitation verification fallback and corrected CCPA retrieval. See [release notes](RELEASE_1_4_31.md).

## v1.4.26 — HRRR native psfc selection for below-ground terrain masking

Earlier HRRR surface pressure changes remain in the changelog.

# OMD EVU Converter

**Current release: v1.4.31** · See [release notes](RELEASE_1_4_31.md) and the [changelog](CHANGELOG.md).

The converter downloads supported forecast and verification products, extracts requested GRIB2 fields, writes browser-readable Zarr v2 datasets and JSON catalogs, and publishes data to a configurable web server for the companion model viewer. It runs on a processing server with Python 3.10+, `wgrib2`, and the site-provided runtime environment. No source data, credentials, or local deployment configuration are included in the archive.

Model guidance includes RRFS, HRRR, REFS members and the operational REFS Mean. Available verification datasets include RAP, RTMA, URMA, MRMS, Stage IV precipitation, NOHRSC and observational feeds. Native and aligned analysis stores serve different purposes; an available local dataset must still be published before it can appear in the web viewer. Optional fields are stored only when supported by the upstream source inventory.

The package includes an incremental scheduler, bounded parallel model conversion and analysis alignment, historical case creation, protected-case retention, sounding profile collection, optional GLM lightning collection, audit utilities, and controlled publication. Default unprotected retention is **seven days**; protected cases are excluded from routine cleanup. Saved cases are maintained separately from real-time collection.

**Native shear inputs (v1.4.1):** Forecast-model 0–1 km and 0–6 km shear magnitudes are derived from the native UPP GRIB2 shear-component fields, not calculated from pressure-level winds. Native RAP 0–1 km shear components are now ingested alongside 0–6 km components. Scalar magnitude and component normalization are calculated from those native records; data missing from GRIB2 remain missing. Existing RAP case stores may be updated by running a `--mode missing` case pass.


## Comprehensive retention and cleanup (v1.4.19)

Daily maintenance now retries at each eligible model poll after 03 UTC until it finishes successfully; a skipped 03:00 poll no longer prevents cleanup. The **seven-day rolling UTC retention period** is applied to all recognized generated datasets on **both the processing server and published web server**, except explicitly protected historical cycles and their F000–F084 verification hours. The combined sweep includes forecast Zarrs, sounding profiles, native and aligned analyses (including MRMS and Stage IV), METAR/mPING observation JSONs, SPC reports, GLM lightning, dated verification/sounding scratch, leftover publication archives and catalog uploads, dated case-stage logs, ordinary converter logs, and performance diagnostics. It also reconciles catalogs and publication-state metadata. Operational configuration, current files, symlinks, locks, and unrecognized data are deliberately not deleted. Active uploads and other temp files younger than the cutoff are not removed.

A one-time dry-run may be used to inspect the existing backlog before deleting anything, particularly after deploying this release. From the installation directory, use the configured output, work, publication and protection paths (or supply these explicit flags):

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date "$(date -u +%Y%m%d)" --cycle 00 --models all \
  --cleanup-only --cleanup-dry-run --retention-days 7 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --logs-dir "$PWD/logs" \
  --protected-cycles-file "$PWD/protected_cycles.json" \
  --publish-host 'user@web-host' --publish-dir '/absolute/path/to/data'
```

Review the `WOULD REMOVE` / `WOULD PRUNE` output, then rerun **without `--cleanup-dry-run`** to apply. An SSH login is required for remote cleanup; use `--no-publish` for a local-only dry-run. If the operational protected-cycle JSON is absent or invalid, cleanup intentionally refuses to proceed. Automatic maintenance resumes with the normal cron schedule and retries after transient failure. This script does **not** contact either server from a release archive; the commands must be executed in the deployed processing environment.

## Delayed Stage IV precipitation verification (v1.4.18)

The verification pipeline retains hourly **MRMS** precipitation analyses as the immediately available verifier. It additionally collects NCEP **Stage IV CONUS 1-hour precipitation** GRIB2 files from the configured public archive. Collection is delayed conservatively by 100 minutes from the analysis valid time; recent Stage IV hours are retried during incremental verification runs. The default Stage IV lookback is 120 hours (`--stageiv-lookback-hours`), to permit later replacement of complete cycle-to-date accumulation windows. It must fit within the configured retention period.

Stage IV's native HRAP polar stereographic grid is regridded with `wgrib2` to a 3-km CONUS Lambert grid before Zarr conversion, with explicit `stageiv_apcp` source metadata. Native and aligned Stage IV Zarr products are published separately from MRMS so the viewer can choose **Stage IV only after every required one-hour accumulation is available**; otherwise it continues using complete MRMS windows. Partial intervals and mixed-source sums are not shown. MRMS remains the source for precipitation rate and reflectivity.

Only the **native 1-hour Stage IV APCP product** is collected. Longer viewer accumulations (3/6/24/48 hours and cycle-to-date) are assembled from contiguous hourly analyses, not from Stage IV's separately published 6-hour or 24-hour mosaics. In unusual cases when neither complete Stage IV nor complete MRMS is present, the viewer reports the interval as unavailable instead of estimating it. Difference maps require the corresponding aligned-analysis stores to be published.

For testing, run the existing verification pipeline with `--source stageiv --start-hour YYYYMMDDHH --end-hour YYYYMMDDHH --no-publish` and inspect the resulting 1-hour Stage IV catalog and aligned-analysis output before enabling full publication. Operational credentials, protected cases, configuration and active cron locks are not included in the archive and must be preserved during deployment.

## Performance monitoring

Both cron wrappers now create a timestamped invocation log immediately at startup, before runtime/configuration checks and lock acquisition. Each log records start, completion, skip/failure outcome, exit status, elapsed time, host, and PID. Run `./check_cron_status.sh` to inspect the latest five invocations of the hourly, model, and GFS workflows. The GFS status check recognizes start/end markers even if an SSH login banner is concatenated onto the same line, and distinguishes successful, failed, skipped, disabled, and incomplete polls. If a scheduled time has no corresponding START record, the wrapper was not launched (or the log directory was unavailable); inspect the installed crontab and host cron-service logs.

The converter includes a standard-library-only process-tree monitor. It is enabled by default through the shared `converter_config.sh` setting `EVU_PERF_ENABLED=1` and does not change worker counts or throttle processing. Both cron wrappers write whole-run samples, while Python entry points and historical `create_case.py` runs add task/stage timings. Reports are written to `logs/performance/` (or `EVU_PERF_LOG_DIR` when configured): JSONL resource samples, JSONL stage/transfer events, and concise JSON/text summaries. The default sample interval is five seconds and report retention follows the configured seven-day data window by default (`EVU_PERF_RETENTION_DAYS`). Set `EVU_PERF_ENABLED=0` to disable monitoring. No `psutil`, `h5py`, or additional package is required.

CPU is reported in core-equivalents and as a percentage of the host's logical CPUs. Process-tree RSS is summed across processes and can count shared pages more than once; disk I/O comes from Linux `/proc` process counters. HTTP download and SSH/SCP transfer bytes/timings and concurrent transfer counts are instrumented at the application level. Host-wide resource limits are not enforced by the monitor.


## Documentation

- [Current release notes](RELEASE_1_4_31.md) — CCPA 3/6-hour data and GFS-aligned NOHRSC/CCPA observation analyses
- [Performance monitoring](PERFORMANCE_MONITORING.md) — interpreting automatic CPU, memory, I/O, task, compression, download, and publication reports
- [Case studies](CASE_STUDIES.md) — protected historical case processing
- [Sounding profiles](SOUNDING_PROFILES.md) — pressure-level collection and diagnostics
- [RAP field audit](RAP_FIELD_AUDIT.md) — upper-air and CAPE inventory checks
- [Shear repair](SHEAR_REPAIR.md) — targeted derived-field maintenance
- [Changelog](CHANGELOG.md) — historical changes (without separate outdated release files)

## Current shear and severe-weather overlays

Native RAP 0–6 km bulk shear is calculated with paired, unit-aware U/V
components. The components are normalized to m/s and retained alongside the
scalar magnitude, avoiding the very large values caused by scaling an already
integrated wind difference by 6000 m. Native Bunkers right-moving USTM/VSTM
components are extracted for RAP and forecast models wherever supplied by the
source GRIB inventory. The companion viewer draws bulk-shear wind barbs on bulk
shear fields and right-mover wind barbs on SRH fields, with a persistent overlay
switch. Previously published native RAP shear requires verification backfill
and republishing; merely replacing the viewer is insufficient. See
[SHEAR_REPAIR.md](SHEAR_REPAIR.md).

## RRFS / REFS-member low-level bulk shear (v1.4.8)

The source 0–1 km shear components were independently found inconsistent with native vertical wind profiles. RRFS and REFS members now derive **1-km AGL minus 10-m** U/V wind differences using native 3-km pressure-level winds, heights, and surface pressure. Invalid pressure or insufficient above-ground profile coverage yields missing values. The derived map components and magnitude use the same m/s arrays (viewer labels them in knots). HRRR, RAP, and all 0–6 km shear calculations are unchanged.

To repair an existing historical case without repeating soundings, analyses, observations or unrelated model fields, preview and then execute `python3 create_case.py --cycle 2026102812 --lowlevel-shear-only [--execute]`. See `SHEAR_REPAIR.md` and the historical entries in `CHANGELOG.md` for limitations and validation requirements. This case mode retains the regular cron coordination and publication logic.

## Case/cron coordination (v1.4.7)

A historical `create_case.py --execute` run **waits for already-running cron jobs** and then runs exclusively. There is no need to edit, disable or re-enable cron entries. Once a case starts waiting, each new model or hourly verification cron invocation logs that it was skipped and exits successfully; its **next scheduled invocation** retries normally after the case completes. The two cron wrappers are still permitted to overlap with each other. Locks release automatically after the case and any inherited in-flight worker processes exit, including an interrupted case. Do not delete lock files manually.

Deploy `case_cron_coordination.sh` with **both** updated cron wrappers (`run_model_incremental_cron.sh`, `run_multimodel_cron.sh`) and `create_case.py`, on a filesystem supporting cross-process and cross-node `flock`. Confirm that actual crontab entries invoke these wrappers and not a direct converter command. Case dry-runs are nonblocking and take no locks. If a case is launched while an older cron wrapper is still executing, its legacy writer lock is also honored.

**Important packaging correction:** Previous v1.4.6 ZIPs inadvertently bundled hidden `.lock` files; overwriting such files during an active cron invocation could detach the old lock inode from new arrivals. Current packages never contain locks. Do not unzip a package while active case/cron commands are running, and never delete or overwrite lock files manually. The release builder excludes them by construction and verifies the final ZIP.

The case locks now resolve from the **operational protected-cycle directory**, rather than the directory holding the Python script. Both installed cron wrappers must declare the same `BASE_DIR`; when that directory exists, a mismatch causes the case to **abort safely before processing**, instead of mistakenly acquiring unrelated lock files. In an unusual deployment, specify `--lock-dir /path/to/cron/BASE_DIR` and use the **same actual protected-cycle whitelist** as cron. Logs print `Case/cron lock directory: ...` before waiting; this must equal both cron wrappers' `BASE_DIR`. Do not delete lock files manually or replace running scripts. Lock tests on another node are recommended when jobs may use distinct hosts; locally passing tests do not prove a network filesystem propagates `flock` between nodes.

## Unified cron/case worker configuration (v1.4.3)

The trusted local `converter_config.sh` is read automatically by `create_case.py`, using the same configuration file already sourced by both cron launchers. This is the converter-wide operational configuration for model and analysis cron jobs, soundings, observations, GLM collection, publication, retention, and historical cases—not just the incremental model watcher. Shared settings include worker limits, retention, `NO_PUBLISH`, model selection, output/work roots, publication host/directory, protected-cycle path, observation-secrets path, and optional GLM case settings. It may be overridden with `--config-file /path/to/file` or `CONVERTER_CONFIG_FILE`. `INCREMENTAL_CONFIG_FILE` remains a deprecated compatibility alias. If only the old `incremental_model_config.sh` exists, both cron wrappers and `create_case.py` continue to read it with a migration notice; rename it to `converter_config.sh` when convenient. The deployment-specific configuration is not included in release packages; `converter_config.example.sh` is a complete starting template. Preserve existing private settings when upgrading.

The precedence for historical case worker settings is **CLI → `CASE_*` → cron-shared name → built-in default**. Available case-only variables are `CASE_WORKERS` (fallback `INCREMENTAL_WORKERS`, then `WORKERS`), `CASE_STAGE_WORKERS`, `CASE_SOUNDING_WORKERS` (fallback `SOUNDING_PROFILE_HOUR_WORKERS`), `CASE_ALIGNED_WORKERS` (fallback `ALIGNED_WORKERS`), and `CASE_PUBLISH_WORKERS` (fallback `PUBLISH_WORKERS`). Worker values and their sources are reported before the dry-run or execution. No separate `source` command is required.

The shipped example uses **18/4/4/4/4** for forecast/stage/sounding/aligned/publishing case workers, and **16/4/4/4** for incremental forecast/sounding/aligned/publishing cron workers. All converter subprocesses use one OpenMP/BLAS thread in a case, matching cron. These are tunable concurrency settings, **not strict system utilization limits**: a 20% CPU or memory target must be verified with system monitoring because GRIB decoding memory varies substantially by field, grid and source.

## Dependency-aware workflow scheduling (v1.4.0)

Historical case creation now executes independent subprocess stages concurrently
(**`--stage-workers 3`** built-in default, any positive integer allowed), without overlapping writers
to the same store catalog. Forecast processing/publication, native analysis,
sounding-profile extraction, and observation/SPC collection can make progress
independently. An aligned-analysis batch starts only after its native-analysis
window succeeds; successive aligned catalog updates are serialized. Forecast
model publication follows a successful conversion stage, while forecast and RAP
profile stages share a single serial catalog-writer chain. Any failed dependency
skips its downstream stage and causes a nonzero case exit, but independent work
continues. The existing cron locks, protected cases, data formats and atomic
publication rules are unchanged.

For auditability, concurrent case subprocess output is separated into mode-0600
files in `work/case_stage_logs/<cycle>_<timestamp>/`, while the console reports
stage starts, finishes, failures, and skips. `--stage-workers 1` restores the
serial workflow, streaming all subprocess output to the terminal. This stage
limit is **separate from** `--workers`, `--aligned-workers`, and
`--publish-workers`; start with the configured stage count and monitor CPU, memory,
network and filesystem loads before raising it.

Sounding-profile hours can also be processed concurrently within one model:
`create_case.py --sounding-workers 3` (built-in default, any positive integer allowed) passes
`--hour-workers 3` to `sounding_profiles.py`. Each hour has a separate
working directory, array chunks and completion stamp; the catalog update
remains serialized. A final missing-only profile reconciliation runs after
model conversion to recover hours that became ready during the initial
parallel pass. The sounding processor reports start and elapsed processing
seconds for each hour. Large native GRIB2 downloads and decodes still require
considerable time, and multiple workers consume more memory and bandwidth.
For resuming an interrupted case, use `--mode missing` **without**
`--reprocess-soundings` unless an intentional complete profile refresh is
required. This preserves all successfully completed profile hours.

In the 15-minute model cron, forecast and native sounding stages may overlap
(one model writer and one profile writer per cycle), while profile mean/member
processing and final retention remain ordered. Set
`INCREMENTAL_PARALLEL_STAGES=0` to restore serial operation. The incremental
cron also accepts `SOUNDING_PROFILE_HOUR_WORKERS` (default 2), although its
normal one-hour-per-model batches rarely need multiple decoding workers. In the hourly
wrapper, authorized observation and independent SPC report transfers may
overlap; `HOURLY_PARALLEL_REPORTS=0` disables that overlap. The hourly native
verification → aligned-analysis dependency remains sequential to ensure
alignment uses complete native analyses. The two existing cron wrappers already
operate independently under separate locks.

## Publication throughput

Set `PUBLISH_WORKERS=3` (default; allowed 1–6) in the applicable runtime
configuration. Independent model, native analysis and aligned-analysis stores
can transfer concurrently. A private temporary SSH control socket reuses
connections across SCP/SSH operations; failed transient connections are retried
up to three times with bounded backoff. Transaction-specific catalogs are
installed after their corresponding Zarr files, and native/aligned aggregate
catalogs only after every referenced store transfer succeeds. Cleanup remains
serialized and protected cases retain their normal safeguards. Set
`PUBLISH_WORKERS=1` to disable the parallel transfer pool. The case command also maintains one private SSH control connection across its subprocess stages, and passes
`--publish-workers` to the aligned-analysis publisher as well as all other
Zarr publication stages. Hourly observation and daily SPC-report batches share
a private SSH control connection and create their remote destination directory
once per successful batch, rather than before each report file. Each report
still installs atomically. These improvements also apply to the existing cron
collectors automatically, with no new configuration or dependencies. Existing
changed-file/delta publication for models is unchanged, avoiding repeat full
uploads when an acknowledged publication snapshot exists. No new third-party
Python packages or cron entries are required.

## Upper-air, shear, and retention capabilities

- Adds **300-hPa U/V winds, geopotential height, and derived wind speed** for forecast models, and RAP F000 300-hPa wind/height fields, including aligned analysis handling. Existing completed cycles can be backfilled with `--repair-upper-air` and `--publish-only`.
- Corrects a bulk-shear conversion pitfall: RRFS/REFS native GRIB VUCSH/VVCSH components may be wind differences (m/s), whereas HRRR/RAP may supply vertical gradients (s^-1). Unit-aware derivation inspects magnitudes and source conventions and rejects implausible speeds. Already-written shear can be recomputed using `repair_bulk_shear.py` without any network downloads. See [SHEAR_REPAIR.md](SHEAR_REPAIR.md).
- Applies **seven-day retention** to dated incremental and hourly logs, rotated legacy cron stderr logs, observation GeoJSON files, and expired per-store publish snapshots on the processing server. Remote cleanup additionally removes expired observation GeoJSON, abandoned model-transfer archives, and obsolete staging directories. Valid hours from protected model cycles and protected analysis ranges remain pinned. Static code, configuration, catalogs, current jobs, locks, and credentials are never swept. The consolidated daily cleanup now also covers native/aligned analysis and sounding stores and their dated processing artifacts.
- The existing `run_model_incremental_cron.sh` daily cleanup now retries after 03 UTC until complete, even when a scheduled poll was skipped. No cron entry changes are needed.

### NOHRSC snowfall metadata handling

NOHRSC 6-/24-hour snowfall conversion now reads native GRIB variable units from the bracketed descriptions returned by `wgrib2 -V` (for example `[m]`, `[mm]`, `[cm]`, or `[in]`). Previously, the converter expected a nonexistent `units=` key and rejected otherwise readable NOHRSC products. Unsupported/unknown physical units remain rejected; input depth units are converted to meters before writing the viewer-compatible Zarr arrays. Failed earlier hours can be retried with the normal verification backfill/cron process; existing model stores and scheduled cron entries are unchanged.

### Case-study creation

The new `create_case.py` coordinates a single initialization cycle or inclusive six-hour cycle range, restores missing or explicitly overwritten model fields, generates and publishes all available forecast and hourly RAP sounding profiles, retrieves native verification/analysis products and aligned analyses for the case valid-time window, and collects time-matched Synoptic METAR/mPING reports. It atomically merges the selected cycles into the operational `protected_cycles.json` (with timestamped backups) before processing, preserving existing case entries. Dry-run preview is the default; `--execute` performs the work, and `--rights-confirmed` is required for authorized observation publication. The utility uses both existing cron locks and does not require any additional cron entry. Existing completed sounding profiles can be refreshed within the same case command using `--reprocess-soundings`; soundings run by default, with optional `--skip-soundings`. Publication honors `--publish-workers` (built-in default 4) and existing delta publishing. See [CASE_STUDIES.md](CASE_STUDIES.md) for complete commands, limitations, and recovery.

### Incremental recovery

The v1.1.1 maintenance release fixes an incremental retry crash (`TypeError` while forming the `repair_*_added` manifest key) when a processed forecast hour is missing some fields and the retry has no named repair mode. Existing Zarr chunks and progress records from an interrupted poll remain on disk. Subsequent polls can recheck missing fields and complete catalog generation and publishing. No rebuild, forced overwrite, cron edits, or data format migration is necessary after replacing the converter Python script. The existing incremental scheduler and verification/observation cron settings remain in effect.

### Incremental availability warning classification

An already completed hour may lack optional fields that are not published by the source model, particularly at forecast hour zero. The incremental model watcher retries these missing fields according to `INCREMENTAL_MISSING_RETRY_MINUTES`. When no matching optional records are supplied, the check is counted under `completed_hours_checked_no_new_fields` in the run summary, without emitting a misleading **hour unavailable** warning or clearing existing Zarr chunks. A *new/unprocessed* forecast hour with no matching fields continues to be reported as unavailable. Other HTTP and conversion failures remain errors.


## Forecast and verification sources

| Data source | Scope |
| --- | --- |
| RRFS | 00/06/12/18 UTC cycles, F000–F084 |
| REFS members m001–m005 | F000–F060 |
| REFS Mean | F001–F060; PMMN composite reflectivity and accumulated precipitation; native mean 500-hPa height/wind, derived 500-hPa absolute vorticity |
| HRRR | F000–F048 |
| MRMS | Hourly composite reflectivity, precipitation rate, and 1-hour quantitative precipitation estimates |
| RAP | Hourly F000 analysis, including MSLP, pressure-level temperature/heights/winds, 500-hPa absolute vorticity, 0–6 km bulk shear, storm-relative helicity and supported CAPE fields |
| URMA / RTMA | Hourly near-surface verification, with URMA preferred when valid-time matched and available |
| Synoptic Data | ASOS/AWOS METAR present-weather classification (hour-matched, optional) |
| mPING | Crowdsourced precipitation-type reports (hour-matched, optional) |
| NOHRSC | Native 6-hour snowfall at 00/06/12/18 UTC and native 24-hour snowfall at 00/12 UTC; approximately 2-hour initial eligibility delay |

The source inventory determines actual field availability. SCP and STP products are not collected or published by this converter. Severe-weather fields use native GRIB2 diagnostics where present: `VUCSH`/`VVCSH` components for bulk shear, `HLCY` for SRH, `MXUPHL` for hourly UH maxima, and other supported inventory-matched severe fields. Accumulated UH maximum is the maximum of validated hourly maxima, not an additive accumulation. An unavailable GRIB record remains unavailable.

## Main workflow

- **Two coordinated cron entries** are used in incremental mode: the original hourly wrapper continues RAP/RTMA/URMA/MRMS, aligned analyses, and authorized Synoptic/mPING observations; `run_model_incremental_cron.sh` polls model sources every 15 minutes. The shared `converter_config.sh` sets `MODEL_SCHEDULER=incremental`, disabling old 04/10/16/22 UTC model work in the hourly wrapper.
- The new model watcher starts checking a cycle approximately **15 minutes after initialization** (configurable), ingests only discovered forecast hours and missing fields, and revisits the prior three cycles. Each pass has a configurable forecast-hour frontier and job budget.
- Completed fields are never overwritten during incremental recovery. The coarse `processed` marker is supplemented by on-disk chunk checks, staged interval awareness, and a per-hour missing-field retry clock (default 60 minutes). Native product gaps may remain missing, with no invented values.
- **First publish is a full bootstrap; subsequent publishes send changed or removed Zarr files plus a catalog.** A successful-transfer snapshot is retained under `work/.published/`; unsuccessful transfers do not advance the snapshot. Changed files are installed atomically and the catalog is published last. `--publish-mode full --publish-only` forces a fresh full-store upload if necessary.
- REFS Mean's `mean` product supplies 500-hPa U/V wind and height but not native 500-hPa `ABSV`. Missing `absv500` is derived from the ensemble-mean winds on the model Lambert grid, adding the spherical metric and Coriolis terms. The Zarr metadata explicitly identifies **DERIVED** provenance; this is a derived physical quantity, not a substituted native GRIB record.
- Established delayed REFS Mean repair continues at 72/96 hours. The model watcher also performs daily retention; protected case studies remain exempt.
- Historical **legacy mode** (one hourly cron and 04/10/16/22 UTC model ingestion) remains available by omitting `converter_config.sh` and the new watcher.
- Verification uses the existing default **20-hour lookback** (`VERIFY_LOOKBACK_HOURS=20`) for delayed URMA and other products. NOHRSC has a separate configurable lookback; only native 6-/24-hour snowfall is downloaded.
- Regular data use **7-day retention** locally and remotely. An operational `protected_cycles.json` maintains explicitly selected cases and their verifying analyses beyond the regular retention period.
- `aligned_analysis.py` remaps available native verification stores to the model grid for efficient model−analysis differences; outputs are published with a separate aligned-analysis catalog.
- Native NOHRSC 48-/72-hour downloads are disabled. The viewer derives 48-hour snowfall from two aligned, complete NOHRSC 24-hour analyses. MRMS 3/6/24/48-hour precipitation analysis is derived from complete hourly records.

## Included source files

| File | Role |
| --- | --- |
| `multi_model_to_zarr_wcoss2.py` | Main forecast conversion, repair, verification dispatch, catalogs and publishing |
| `run_multimodel_cron.sh` | Hourly RAP/verification/observations launcher; legacy model fallback |
| `run_model_incremental_cron.sh`, `converter_config.example.sh` | 15-minute model watcher and shared converter configuration template |
| `check_cron_status.sh` | Show recent hourly, 15-minute model, and 30-minute GFS cron invocations and their outcomes |
| `capture_converter_runtime.sh`, `converter_cron_env.sh` | Captured processing server runtime support |
| `verification_pipeline.py`, `verification_ingest.py`, `verification_convert.py`, `verification_sources.json` | Analysis ingest, conversion, sources and catalog publishing |
| `aligned_analysis.py` | Remapping native analyses to the common model grid |
| `retention_policy.py`, `protected_cycles.example.json` | Shared model/analysis retention policy and template |
| `backfill_model_history.py` | Bounded historical cycle backfill helper |
| `repair_refs_mean_archive.py`, `repair_analysis_access.py` | Targeted maintenance utilities |
| `audit_model_retention.py`, `audit_analysis_alignment.py` | Read-only inspection and auditing |
| `ptype_observations.py` | Optional credentialed METAR/mPING collection, classification and authorized GeoJSON publishing |
| `create_case.py`, `CASE_STUDIES.md` | Safe case-study backfill, publication, and protected-cycle management |
| `tests/` | Development-only regression tests (not included in deployment ZIPs) |

## Deployment on a processing server

All Python source modules and shell wrappers belong together in the established processing directory. Existing `zarr/`, `work/`, `logs/`, SSH configuration, local runtime files and operational `protected_cycles.json` must be **preserved** when upgrading. Site-specific paths and publishing settings in the scripts must be checked before use at a different installation.

A known-working interactive shell can capture the converter runtime:

```bash
cd /path/to/zarr_processing
chmod 750 run_multimodel_cron.sh run_model_incremental_cron.sh capture_converter_runtime.sh check_cron_status.sh
if [ ! -e protected_cycles.json ]; then
  cp protected_cycles.example.json protected_cycles.json
fi
./capture_converter_runtime.sh
```

The generated `converter_runtime_paths.sh` contains installation-local paths and must not be placed in a public repository. A clean environment check can be performed without conversion:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin CHECK_ENV_ONLY=1 \
  /bin/bash ./run_multimodel_cron.sh
```

### Activate the shared configuration and incremental scheduler

After all currently running manual converters or legacy cron jobs have finished, use one local, untracked `converter_config.sh` for both cron workflows and historical case processing. **If `incremental_model_config.sh` already exists, rename it rather than copying the example over it** so all site-specific settings are preserved:

```bash
mv incremental_model_config.sh converter_config.sh
chmod 600 converter_config.sh
```

For a new installation with no existing configuration, create it from the shipped template instead:

```bash
cp converter_config.example.sh converter_config.sh
chmod 600 converter_config.sh
```

The wrappers temporarily retain a fallback to the old filename, but `converter_config.sh` is the canonical name. GLM switches belong in this same file; routine retention follows `RETENTION_DAYS` (there is no separate GLM retention setting).

**Both** entries below must be installed in `crontab -e` (substitute the actual installation path). The hourly entry is unchanged; it now handles only analyses and observations while incremental mode is enabled. The 15-minute watcher handles forecast models and their catalogs:

```cron
10 * * * * /path/to/zarr_processing/run_multimodel_cron.sh >> /path/to/zarr_processing/cron_errors.log 2>&1
*/15 * * * * /path/to/zarr_processing/run_model_incremental_cron.sh >> /path/to/zarr_processing/model_cron_errors.log 2>&1
```

The watcher uses a dedicated `.model_incremental.lock`, so model conversion cannot overlap another model poll; the hourly wrapper retains `.multisource_cron.lock` for analyses/observations. These workflows write different stores and may run concurrently. Manual model conversions must be coordinated with the **model watcher lock**; avoid deploying or performing full backfills during active model writes. A cycle can become visible progressively as forecast-hour GRIBs appear.

Default tuning: `INCREMENTAL_CYCLE_LAG_HOURS=0.25`, `INCREMENTAL_CATCHUP_CYCLES=3`, `INCREMENTAL_LOOKAHEAD=12`, `INCREMENTAL_MAX_HOUR_JOBS=0`, `INCREMENTAL_MISSING_RETRY_MINUTES=60` and `INCREMENTAL_WORKERS=6`. Optional `NO_PUBLISH=1` suppresses remote transfers. Polls are skipped when the previous poll remains active. `logs/model_incremental_YYYYMMDD_HHMMSS_PID.log` is created for every wrapper invocation, including preflight failures and lock/case-priority skips. The hourly wrapper similarly writes `logs/multisource_YYYYMMDD_HHMMSS_PID.log`. Each file has `INVOCATION START` and `INVOCATION END` markers; the end marker identifies `completed`, `skipped`, or `failed`, along with exit status and elapsed seconds. Use `./check_cron_status.sh` for a compact view of the latest five runs of each workflow. A START without END indicates a still-running or interrupted process. The crontab-level `cron_errors.log` and `model_cron_errors.log` redirects remain as a fallback if the logs directory cannot be opened.

The packaged `converter_cron_env.sh` wrapper is required by both cron launchers; it sources the local `converter_runtime_paths.sh` generated by `capture_converter_runtime.sh`. The generated runtime file contains installation-specific executable/library paths and remains private. Model conversion has no new Python dependency. The optional GLM collector uses the existing `netCDF4` package; `h5py` is not required. No changes to the viewer Zarr reader are needed.

**First deployment:** The first successful model publish for each store performs a full-store transfer to establish the persistent publication snapshot; subsequent runs transfer only changed files. A forced `--publish-mode full` can re-seed an existing web server store. The automatic 72/96-hour REFS Mean repair and daily model retention remain active. The hourly verification and credentialed observation collection continue unchanged.

### Selected configuration

`RETENTION_DAYS=7`, `INCREMENTAL_CATCHUP_CYCLES=3`, `INCREMENTAL_LOOKAHEAD=16`, `INCREMENTAL_MAX_HOUR_JOBS=0`, `INCREMENTAL_WORKERS=16`, `VERIFY_LOOKBACK_HOURS=20`, `ALIGNED_ENABLED=1`, `ALIGNED_WORKERS=4`, `PUBLISH_WORKERS=4`, `GLM_ENABLED=1`, `GLM_LOOKBACK_HOURS=3`, `GLM_WORKERS=4`, and `GLM_PUBLISH=1` are represented in the shared example. `GLM_CASE_ENABLED=1` additionally enables historical GLM backfill during `create_case.py`; it defaults to 0 to avoid unexpectedly large historical downloads. The converter and verification source settings contain further options.

### Manual validation and repair

The following commands illustrate limited local tests; a production publishing pass omits `--no-publish`:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date YYYYMMDD --cycle 12 --models rrfs --hours 0-3 \
  --workers 2 --output-root "$PWD/zarr" --work-root "$PWD/work" --no-publish

python3 multi_model_to_zarr_wcoss2.py --verification-only \
  --source nohrsc --lookback-hours 20 --nohrsc-lookback-hours 72 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --no-publish

python3 multi_model_to_zarr_wcoss2.py \
  --date YYYYMMDD --cycle 12 --models rrfs --repair-upper-air \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --no-publish
```

In incremental mode, missing field chunks within previously processed forecast hours are retried on a cooldown; existing completed field chunks are never overwritten. Legacy mode skips completed hours by default. `--repair-mslp`, `--repair-snowfall`, `--repair-upper-air`, `--repair-refs-mean` and `--repair-refs-pmm-apcp` provide targeted recovery; the PMM APCP repair changes the source of precipitation for existing REFS Mean stores and requires backups and independent validation. Manual backfills should run while the hourly cron is idle or under the same shared lock. `--publish-only` can retry publication of an already converted store without repeating downloads; `--publish-mode full --publish-only` forces a full remote re-seed.

### Optional observed precipitation types

`ptype_observations.py` can retrieve time-matched present-weather observations from two authenticated providers and publish **sanitized hourly GeoJSON** into `data/ptype_observations/` for Viewer 1.0.2. The collector uses Synoptic's **ASOS/AWOS METAR network 1**, excluding high-frequency synthetic METARs (`hfmetars=0`), with native `weather_cond_code` or `weather_condition` classification. It also retrieves mPING `Rain/Snow` reports and maps their descriptions to rain, snow, freezing rain, sleet, or mixed. Reporting windows are **±30 minutes** about the UTC valid hour; there is no extrapolation into future forecast valid times. These are discrete reports, not gridded analyses.

**Credentials and publishing safeguards**

- Neither provider's credential is included in this ZIP, stored in the web viewer, placed in the public JSON, or transmitted through browser requests.
- Create a *local only* `ptype_obs_secrets.sh` in the processing directory, containing `export SYNOPTIC_API_TOKEN='[private token]'` and `export MPING_API_KEY='[private key]'`, replacing bracketed placeholders with operational credentials. Set `chmod 600 ptype_obs_secrets.sh`. This file is ignored by the repository and must never be published.
- Put non-secret `PTYPE_OBS_*` switches in the shared `converter_config.sh` so both cron workflows and case operations use one operational configuration. `ptype_obs_config.sh` remains a migration fallback: any non-secret `PTYPE_OBS_*` value explicitly set in the shared config overrides its legacy counterpart, while unset values may still be supplied by the old file. Once migrated, the legacy file can be retired. For routine collection, set `export PTYPE_OBS_ENABLED=1`. To publish to the web server, `export PTYPE_OBS_PUBLISH=1` and `export PTYPE_OBS_RIGHTS_CONFIRMED=1` are *both* required. These switches are intentionally **off by default**, pending confirmation of Synoptic's map-display license/attribution and applicable mPING API terms.
- Both cron launchers read the shared operational configuration and then the separate mode-0600 secrets file. It treats the legacy `ptype_obs_config.sh` as a fallback only; explicit values in the shared config always take precedence. The authorized observation collection remains hourly and is independent of the 15-minute model watcher. If credentials are not installed, observation collection is skipped.

For a local-only test, source the private secrets in a trusted processing server shell and collect one already completed UTC valid hour:

```bash
set -a; source ./ptype_obs_secrets.sh; set +a
python3 ptype_observations.py --date 20261028 --hour 12 \
  --output-root "$PWD/zarr"
unset SYNOPTIC_API_TOKEN MPING_API_KEY
```

The resulting `zarr/ptype_observations/2026102812.json` can be inspected for category/source counts. Publication occurs only with `--publish --rights-confirmed` after explicit authorization; the `--start YYYYMMDDHH --end YYYYMMDDHH` options provide a bounded 168-hour historical backfill. The default cron collection revisits the last three **completed** hourly windows to catch delayed reporting. Provider failures retain previously validated reports for the failing provider; an all-source failure with no prior reports produces an error, not an invented empty data file. Before publishing, ensure web server access and applicable provider terms permit redistribution of the sanitized point reports.

### Mean sea level pressure and RAP F000 fields

Forecast MSLP is retrieved from the available native GRIB2 sea-level reduction (`PRMSL`, `MSLET`, or `MSLMA`), not surface station pressure (`PRES`). The existing GRIB2 product inventories are consulted for each model and hour. The viewer converts pascals to hPa and always plots labeled isobars independent of the temperature-contour setting.

RAP analysis ingestion extracts sea-level `MSLMA` from the existing `awp130bgrbf00` source and pressure-level temperature, height and U/V wind components from the existing `awp130pgrbf00` source. RAP F000 also supplies 500-hPa absolute vorticity, 0–6 km shear (`VUCSH`/`VVCSH` vector magnitude times 6000 m), 0–1 and 0–3 km storm-relative helicity, surface CAPE, lowest-90-hPa mixed-layer CAPE and lowest-180-hPa most-unstable CAPE. RAP analysis is model-based, not independent observation. Fields not found in the exact operational F000 inventory are not fabricated. The primary `awp130pgrbf00` inventory omits 0–1 km shear; the converter checks native `wrfprs` GRIB records and safely regrids the selected shear/storm-motion fields when available. An explicitly derived 0–1 km estimate is a fallback when native acquisition is unavailable. Unsupported UH analysis fields remain unavailable.

Previously completed **forecast** stores can acquire the new MSLP field with a targeted, non-destructive local repair:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261028 --cycle 12 --models all --repair-mslp \
  --workers 2 --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --no-local-cleanup --no-publish
```

After local verification, run `--publish-only` with the same cycle and models to publish updated model stores and catalogs. Existing hourly RAP analysis stores will not automatically gain new fields: the verification pass normally skips already converted valid hours. A bounded, explicitly forced RAP backfill regenerates them (when the original source products are still accessible):

```bash
python3 multi_model_to_zarr_wcoss2.py --verification-only \
  --source rap --lookback-hours 72 --force \
  --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --no-local-cleanup --no-publish
```

After validation, rerun without `--no-publish` to publish refreshed RAP analyses and their catalog. To regenerate the model-grid aligned RAP fields, run `aligned_analysis.py` with `--products rap_surface,rap_pressure --since YYYYMMDDHH --max-new-hours 120` using existing deployment-specific arguments. Model and verification repairs must not overlap an active hourly cron invocation.

### Repair of missing accumulated snowfall (ASNOW)

**Version 1.0.1** recognizes both hour- and day-based accumulation interval metadata in operational GRIB2 indexes. Prior converter versions could stage legitimate `0–1 day acc` / `0–2 day acc` / `0–3 day acc` ASNOW records as `unknown`, leaving F024/F048/F072 missing even though unrelated forecast fields and shorter snowfall intervals were available. This primarily affects forecast models other than REFS Mean.

**Stop the hourly cron entry before manual backfill** and retain all existing Zarr stores, manifests, protected-cycle rules, and operating configuration. The targeted repair retains existing field chunks and never replaces already present ASNOW data. An original GRIB inventory line is required to recover a staged accumulation locally; unverified staged records are not assumed cumulative. If staged material is missing or cannot be validated, only the snowfall GRIB field is acquired again. Correct unit metadata and a matching interval endpoint are required. The repair does not synthesize missing accumulations.

Example local-only repair of one model and initialization cycle:

```bash
cd /path/to/zarr_processing
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261028 --cycle 12 --models hrrr \
  --repair-snowfall --workers 2 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --no-local-cleanup --no-publish
```

For a cycle with all models, substitute `--models all`. The full model-specific forecast-hour range is checked unless `--hours` is provided. To target only known day-based accumulation endpoints, append `--hours 24,48,72` (HRRR ends at F048; REFS at F060). A complete backfill should include any other missing ASNOW hours, as needed.

After verifying the ASNOW coverage in model-specific catalogs and manifests, publish the repaired store without redownloading:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261028 --cycle 12 --models hrrr \
  --publish-only --no-local-cleanup --no-remote-cleanup \
  --output-root "$PWD/zarr" --work-root "$PWD/work"
```

A batch over several days may use the same repair command within UTC date (`YYYYMMDD`) and cycle (`00`, `06`, `12`, `18`) loops, with cron disabled and with disk capacity reviewed before publishing. Only 00/06/12/18 UTC HRRR cycles can extend to F048; HRRR does not forecast F072. The viewer already recognizes `asnow` at these forecast times, so **no viewer code change is required**.

### Protected cases and historical backfills

A `create_case.py` utility is included for creating preserved evaluation cases. For example:

```bash
# Preview a case without downloading or modifying retention rules
python3 create_case.py --cycle 2026102900

# Process/publish missing forecasts, all valid analyses, and METAR/mPING observations
python3 create_case.py --cycle 2026102900 --execute --rights-confirmed

# Single-step recovery, including previously completed/old sounding hours:
python3 create_case.py --cycle 2026102812 --execute --mode missing \
  --reprocess-analysis --reprocess-soundings --publish-workers 3 --rights-confirmed

# Multiple initialization cycles (inclusive 6-hour sequence)
python3 create_case.py --start-cycle 2026102800 --end-cycle 2026102918 \
  --execute --rights-confirmed
```

The case tool adds the model cycles to the existing operational protection list before processing, backs up the list, uses exact historical verification/observation windows, and retains the normal publication/catalog logic. When `GLM_ENABLED=1` and `GLM_CASE_ENABLED=1` are set in the shared configuration, it also backfills GLM flash-centroid observations for the available case valid hours. `--skip-glm` omits that stage for an individual case. GLM is an optional observation enhancement: an incomplete source hour, download problem, or GLM publication error is reported as a warning and does not fail otherwise successful forecast/analysis case processing. The collector continues through later hours and batches. Complete hourly files are reused; hours without a valid local GeoJSON are retried the next time the same case is executed. See `CASE_STUDIES.md` for site setup, lock requirements, missing-only versus full reprocessing, and explicit local-only operation.


The operational `protected_cycles.json` is intentionally not distributed. The included `protected_cycles.example.json` is a template. A protected model cycle also protects matching verifying analyses from F000 through F084. Missing or invalid operational protection configuration halts cleanup/processing rather than silently dropping the protection policy. Routine retention cannot restore data that has already been deleted.

## Testing and validation

Syntax checks: `python3 -m compileall -q .` and `bash -n run_multimodel_cron.sh`. Targeted Python tests are in `tests/`. Live processing server GRIB availability, publication permissions, source latency, actual optional severe-weather inventory, and browser-visible products require operational verification.

For the complete development history, see [CHANGELOG.md](CHANGELOG.md).

## Source and operational security

Do not commit `zarr/`, `work/`, runtime captures, logs, SSH credentials, or `protected_cycles.json`. The supplied scripts contain installation-specific directory and publishing destination defaults; public repository maintainers should review and replace those defaults where appropriate. The `.gitignore` does not retroactively untrack files already committed to Git.


## Targeted maintenance

**Preview 7-day cleanup without deletion** (runs the local and web server retention checks; requires a valid operational `protected_cycles.json`):

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261030 --cycle 00 --models all --cleanup-only --cleanup-dry-run \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --logs-dir "$PWD/logs" \
  --protected-cycles-file "$PWD/protected_cycles.json"
```

The 03 UTC incremental cron maintenance performs the real cleanup automatically. Legacy `cron_errors.log` and `model_cron_errors.log` stdout/stderr captures are rotated to dated files in `logs/` if nonempty; those dated logs and the per-invocation `multisource_*.log` / `model_incremental_*.log` files are then expired after 7 days. The script never deletes arbitrary files or directories just because they are old. Source trees, environment files, access keys, protected cases, catalogs, and lock files are not retention targets.

Hourly `lightning/YYYYMMDDHH.json` files are included in the same local and remote retention pass. Expired GLM hours are removed after `RETENTION_DAYS` unless their valid hour is covered by a protected case/analysis window. Stale interrupted GLM upload files are also removed by age; active/recent temporary files, symlinks, unrelated files, and protected hours are preserved. The same policy applies to local and published web-server data.

**Check source availability of native severe-weather indices:**

```bash
python3 audit_severe_fields.py --date 20261030 --cycle 00 --hour 12 --models all
```

**Repair already-ingested bulk shear:** see [SHEAR_REPAIR.md](SHEAR_REPAIR.md). Run while both crons are idle; publish the affected model cycles afterward. No GRIB refetch is needed when component chunks already exist.

### REFS Mean sounding-source selection

The sounding collector uses the published REFS Mean GRIB2 `mean` product directly. It includes time-lagged guidance not represented by the five separately displayed REFS members. REFS Mean has native temperature, dewpoint, and U/V wind at sparse isobaric levels (four complete levels at 925, 850, 700, and 500 hPa). It does **not** include specific humidity; direct DPT is retained instead. Missing vertical data are not interpolated. For operational settings, inventories, and manual backfills see [`SOUNDING_PROFILES.md`](SOUNDING_PROFILES.md).

## Verification inventory updates

An existing hourly verification store is reused only when its source GRIB staging
manifest accounts for the **current** selected field list (selected or explicitly
missing) and its recorded native layer selectors still match. This allows RAP
300/500-hPa fields and other subsequently added requests to be recovered by a
date-targeted verification backfill. See [CHANGELOG.md](CHANGELOG.md).


### Storm motion and low-level shear validation (v1.4.4)

If HRRR storm-motion barbs display but RRFS/REFS barbs are absent, confirm the
latest converter is installed. v1.4.4 accepts both native storm-motion layer
orders; use case `--mode missing` to fill native missing components from still
available sources. Reprocessing existing valid forecast shear is not required
solely for storm-motion ingestion.

When RRFS/REFS and HRRR 0–1 km shear differ substantially, inspect the **local**
Zarr chunks before applying any rescaling:

```bash
python3 audit_storm_fields.py --root "$PWD/zarr" --cycle 2026102812 --hour 12
```

The audit prints median/p90/p99 shear in knots and compares scalar magnitudes
to their native U/V components; ratios near 1 imply m/s wind differences,
whereas ratios near 1000 or 6000 imply native s^-1 gradients. A consistent
component/scalar ratio does not itself establish which forecast is correct.

The standard RAP `awp130pgrb` native analysis source generally offers 0–6 km
but not 0–1 km vector shear; absence of RAP 0–1 km is not automatically an
ingestion error. A different native RAP GRIB product has 0–1 km shear records,
but cross-product ingestion is not enabled in this release.

## Catalog and sounding surface improvements (v1.4.5)

Case runs now honor configured native-analysis retention when publishing and reconcile local upload state with remote completed stores. A remote-missing analysis store is re-published from local data if it is within retention or protected; expired unprotected local history stays intact but is not advertised remotely. Existing protected records remain protected.

Sounding products now include native surface pressure and 2-m temperature/dewpoint when available. Legacy profile hours receive a surface-only data refresh during a missing-data pass; pressure-level profile chunks are retained. A matching updated viewer is required to use these new fields for below-terrain filtering and plotting temperature/dewpoint at the native surface pressure. Missing native fields are reported as unavailable.

Forecast bulk shear now retains normalized U/V components consistent with the published magnitude for both depth layers. Historical native source files may need to be revisited once in `--mode missing` to update older shear components. RAP 0–1 km is preferentially sourced from regridded native `wrfprs` records; an explicitly labeled estimate uses existing terrain-aware wind profiles only when the native source is unavailable.


## REFS Mean probability-matched products

REFS Mean composite reflectivity uses the probability-matched mean (PMM) product and is labeled **PMM composite reflectivity**. REFS Mean accumulated precipitation uses the PMM precipitation source when the PMMN product is available and is labeled **PMM accumulated precipitation**. The targeted `--repair-refs-pmm-apcp` option updates existing REFS Mean precipitation stores from the PMM source; old ordinary-mean data must not be relabeled as PMM without that source repair.

## Optional GLM lightning observations (v1.4.9+)

The collector writes web-readable hourly GLM JSON (`0644`) and publishes it into a `lightning/` directory (`0755`), including explicit remote staging-file permission correction. Cached valid hours generated by older releases are still eligible for publication and will be published with corrected remote permissions.

`glm_lightning.py` collects satellite-observed optical flash centroids from the preceding UTC hour into per-valid-hour GeoJSON for the viewer's **GLM lightning** overlay. It is independent of forecast conversion, default-disabled in unified cron (`GLM_ENABLED=0`), and requires the existing `netCDF4` package in Python 3.10+. `h5py` is not required. The collector prefers the in-memory NetCDF4 reader and falls back to a temporary file if the linked netcdf-c build does not support memory reads. Because netCDF-C is not thread-safe, GLM_WORKERS controls concurrent HTTP downloads only; NetCDF4 Dataset open/read/close operations are serialized in the collector thread. Download payloads are bounded to the active worker window to limit memory use. No API token or new cron entry is required. `GLM_ENABLED=1` opts in; `GLM_LOOKBACK_HOURS`, `GLM_WORKERS`, `GLM_PUBLISH` tune processing, and `NO_PUBLISH=1` overrides publishing. Standalone collection example: `python3 glm_lightning.py --date 20261030 --hour 18 --publish`. For historical ranges use `--start YYYYMMDDHH --end YYYYMMDDHH --publish`. For details and the important distinction between missing GLM data and no lightning, see the changelog.

## GFS v16 / GFS v17: native-resolution converter and soundings (v1.4.23)

The GFS forecast group is **converter-only** at this stage; a six-panel viewer
will be configured separately. The other forecast, analysis, and observation
jobs continue as before. Run `gfs_to_zarr.py` directly, or use the optional
`run_gfs_incremental_cron.sh` poller as an independent 30-minute cron task.

**Native spatial domain:** 0.25° regular latitude–longitude grid,
5°–75°N, 160°–310°E (160°E to 50°W), inclusive. The output is **601 columns
by 281 rows**, ordered west-to-east and south-to-north. This includes
mainland Alaska, the Aleutian Islands, Hawaii, Puerto Rico, and CONUS.
Cropping uses `wgrib2 -small_grib`, **not `-new_grid`**. The converter checks
the resulting dimensions, corner coordinates, and spacing before accepting
the data; there is no 3-km resampling. Coordinates and grid metadata are
stored in Zarr for the future lat/lon viewer implementation.

- **v16 source:** NODD object storage, primary 0.25° GFS atmospheric product
  `gfs.YYYYMMDD/CC/atmos/gfs.tCCz.pgrb2.0p25.fFFF`, with accompanying
  `.idx` files. The converter downloads only selected GRIB message byte ranges,
  not the entire global file.
- **v17 source:** a configurable local base directory containing
  `gfs.YYYYMMDD/CC/products/atmos/grib2/0p25/gfs.tCCz.pres_a.0p25.fFFF.grib2`.
  Message selection is performed from its local `wgrib2 -s` inventory; it is
  not assumed to have exactly the same field inventory as v16.
- **Time:** 00/06/12/18 UTC; **F000 through F240 every three hours**
  (81 forecast times). No one-hour accumulation or updraft-helicity fields.
  The initial field list derives from the existing viewer's RRFS/REFS
  parameters; absent GRIB fields are skipped rather than fabricated.
- **Accumulations:** Supported 3-hour or longer native precipitation intervals
  are reconstructed to run-total form only when the required predecessor is
  available. Staged intervals are retried after missing hours arrive.
  WEASD remains a snow-water-equivalent state field, not an increment.
- **Catalogs:** `catalog_gfs_v16.json` and `catalog_gfs_v17.json`; Zarr stores
  are `gfs_v16_YYYYMMDD_CC00.zarr` and `gfs_v17_YYYYMMDD_CC00.zarr` at the
  configured output root. They are separate from the RRFS/REFS catalog.
- **Native-grid soundings:** Every three-hourly GFS lead is eligible for a
  separate `sounding_gfs_v16_YYYYMMDD_CC00.zarr` or
  `sounding_gfs_v17_YYYYMMDD_CC00.zarr` profile store. Soundings use the **same
  601 × 281 native 0.25° cells as the map**, with `grid_stride=1`. There is
  **no horizontal downsampling or upscaling**. Profiles have native 25-hPa
  *slots* from 1000 to 100 hPa; slots without exact native TMP, SPFH,
  UGRD, VGRD (or supported DPT/RH fallback) remain NaN. Surface pressure,
  2-m temperature/dewpoint, and 10-m winds are included. Profile records are
  selected using the same native GRIB index and spatial crop; they are
  automatically generated with the GFS map conversion, not via a separate job.
- **Kinematic fallbacks:** When native 0–1/0–6 km bulk shear is absent,
  U/V components and magnitudes are calculated from the valid native-level
  wind/height profile using AGL interpolation, terrain height, surface pressure,
  and 10-m winds. GFS v16 missing 0–1 km SRH and other absent native SRH
  are calculated using native right-moving storm-motion vectors **only if**
  the wind profile spans the required layer. Native SRH always wins when
  present. No missing layer or terrain data are invented; unsupported grid
  points remain missing. Vector shear magnitudes are in m/s, SRH in m²/s².
  Provenance is retained per forecast hour. The GFS group still excludes UH.
- **Profile backfill:** A forecast hour previously converted without a
  sounding is automatically revisited in *profile-only* mode. Missing hours
  retry using the standard cooldown. `--skip-profiles` disables this optional
  work for a specific diagnostic run; normal runs and cron enable profiles.
  `catalog_profiles.json` includes the new GFS entries, and local/remote
  seven-day retention preserves profiles for protected GFS cycles.
- **Backfill:** The GFS cron checks the latest eligible cycle and older cycles
  every poll. Incomplete/missing model hours remain pending; a cooldown avoids
  needlessly retrying absent source files every few seconds. An optional
  manual backfill can specify a cycle and selected three-hourly leads.
- **Retention:** Seven days by default on both the processing and web sides,
  using the same protected-cycle JSON as the existing models. Protect a GFS
  cycle before ingesting a historical cycle older than retention.

### Initial manual validation

Run from the installed converter root with its runtime environment and the
operational protected-cycle file available. **v1.4.23 retains the earlier logging fix:** the inventory previously completed silently because INFO messages
were accidentally suppressed. Inventory mode writes INFO diagnostics to
**stderr**. `2>&1 | tee` makes them visible and saves a copy. `--force-hours`
ensures the nominated leads are inventoried even if existing local state says
they were already completed or recently checked.

```bash
# Unit tests (no source access needed).
python3 -m unittest discover -s tests -p 'test_gfs_*.py' -v

# Read two real forecast inventories for EACH model, without converting or uploading.
# If checking a different cycle, change both --date and --cycle.
python3 -u gfs_to_zarr.py --date 20261101 --cycle 18 \
  --models gfs_v16,gfs_v17 --hours 3,6 --max-hour-jobs 2 \
  --inventory-only --force-hours --no-publish --verbose \
  --wgrib2 "$(command -v wgrib2)" \
  --protected-cycles-file ./protected_cycles.json \
  --output-root ./gfs_test_zarr --work-root ./gfs_test_work \
  2>&1 | tee gfs_inventory_test.log
# Bash reports the converter's exit status rather than tee's:
echo "Converter exit code: ${PIPESTATUS[0]}"

# Small, local-only conversion test; no upload and no remote cleanup.
python3 -u gfs_to_zarr.py --date 20261101 --cycle 18 \
  --models gfs_v16,gfs_v17 --hours 0-12 \
  --max-hour-jobs 5 --workers 2 --no-publish --force-hours \
  --wgrib2 "$(command -v wgrib2)" \
  --protected-cycles-file ./protected_cycles.json \
  --output-root ./gfs_test_zarr --work-root ./gfs_test_work \
  2>&1 | tee gfs_conversion_test.log
```

### Validate native-grid profiles for an already converted GFS cycle

Since the previously tested GFS F003 and F006 map hours already exist, do not
force map reprocessing. Run a profile backfill against **the same** local test
output and work roots:

```bash
python3 -u gfs_to_zarr.py --date 20261101 --cycle 18 \
  --models gfs_v16,gfs_v17 --hours 3,6 --max-hour-jobs 2 --workers 1 \
  --no-publish --wgrib2 "$(command -v wgrib2)" \
  --protected-cycles-file ./protected_cycles.json \
  --output-root ./gfs_test_zarr --work-root ./gfs_test_work \
  2>&1 | tee gfs_profile_test.log
```

Check that `gfs_test_zarr/sounding_gfs_v16_20261101_1800.zarr` and
`gfs_test_zarr/sounding_gfs_v17_20261101_1800.zarr` have `t`, `td`, `u`,
`v`, `sp`, `t2m`, and 10-m wind Zarr arrays, and
`.hours/003.json` and `.hours/006.json` completion stamps. For example:

```bash
find gfs_test_zarr -path '*/.hours/00[36].json' -print
python3 - <<'PYCODE'
import json
from pathlib import Path
for m in ('gfs_v16','gfs_v17'):
 p=Path('gfs_test_zarr')/('sounding_'+m+'_20261101_1800.zarr')
 x=json.loads((p/'t'/'.zarray').read_text())
 a=json.loads((p/'.zattrs').read_text())
 print(m, 't shape=',x['shape'], 'grid stride=',a['grid_stride'],
       'finished=',sorted(z.name for z in (p/'.hours').glob('*.json')))
PYCODE
```

Expected shape: `[241, 37, 281, 601]`, stride `1`. A 37-level axis
**does not mean all 37 native levels are populated**; absent pressure-level
records remain NaN. Derived shear and SRH will be populated only where their
required native source levels, surface height, and storm motion are present.
The native-grid pressure-level records increase download, CPU, storage and
publication requirements compared with the initial map-only test; keep
`--workers 1` for this first validation, then tune cautiously.

Expected inventory messages include `selected 2/2 3-hourly leads` for each
model and `F003 has ... matched fields`. A `missing local file` message for
v17, an HTTP fetch error for v16, or zero matched fields requires
investigation; do **not** infer success solely from exit code 0. In inventory
mode only two eligible leads per model are checked. The conversion test should
report the actual fields committed for each available hour; inspect both
model stores, their `.zattrs`, and the generated manifests before enabling cron.

After validation, repeat the conversion without `--no-publish` to publish the
stores and model-specific catalogs.

For operational scheduling, merge the `GFS_*` settings from
`converter_config.example.sh` into the existing private `converter_config.sh`,
then add the new watcher to cron (in addition to the existing RRFS/verification
schedules):

```cron
*/30 * * * * /path/to/converter/run_gfs_incremental_cron.sh
```

The new cron uses the same runtime capture, case-priority gate, protected-cycle
file, output/work roots, retention setting, and publishing destination as the
existing workflows. Its own writer lock prevents overlapping GFS pollers.
Keep `GFS_WORKERS` relatively low (default four, and preferably two with
profiles enabled) until production memory usage
and source latency have been measured. The watcher **does not rerun** analyses,
METAR/mPING, Stage IV/MRMS, GLM, or SPC observations. For manual validation,
start with `--no-publish` and a small `--hours` selection before enabling cron.

When pinning a GFS case, the shared JSON accepts the same form as other models:

```json
{"date":"20261101","cycle":"18","models":["gfs_v16","gfs_v17"]}
```

Place the object inside `protected_cycles` in the existing JSON, retaining
its schema and other entries. The main analysis/observation pipeline remains
unchanged.

**Verification limitation:** The code and synthetic parser/grid tests can be
validated outside the processing environment, but the local v17 files and
actual `wgrib2` conversions must be validated after installation. A failed
cropping grid check prevents writing incorrect native-grid data.

## GFS fixed comparisons and cases

The GFS pipeline materializes fixed native-grid difference Zarr stores and publishes their catalogs. `create_gfs_case.py` saves independent GFS cases and retains verifying observation products through F240.

#### Independent GFS cron-log retention

The GFS poller prunes its own timestamped `logs/gfs_incremental_*.log` files on each eligible run using `RETENTION_DAYS`. This supplements the shared daily retention sweep. Only recognized old files are removed; unknown files, symlinks, and logs recently modified by a running process are preserved. To preview the standalone sweep:

```bash
python3 cleanup_cron_logs.py --logs-dir ./logs --retention-days 7 --prefix gfs_incremental --dry-run
```

The cron output and temporary artifacts outside the known naming patterns are not deleted indiscriminately. Archived cases remain protected according to the case policy.

### Incremental forecast-hour completion (2026-11-03)

Both forecast-model cron wrappers now default to attempting **all currently eligible, incomplete forecast hours** on each invocation. The two hour-selection controls accept `0` to disable the per-run selection cap: `GFS_MAX_HOUR_JOBS=0` and `INCREMENTAL_MAX_HOUR_JOBS=0`. Actual concurrent processing remains limited by `GFS_WORKERS` and `INCREMENTAL_WORKERS`; these are not unlimited-worker modes. Already completed forecast-hour chunks are reused and missing/unavailable inputs remain subject to their existing retry and source-availability checks. A cron invocation that runs past its next scheduled start keeps the existing file lock; the overlapping invocation skips.

The GFS poller still inspects the newest eligible cycle plus `GFS_CATCHUP_CYCLES` older cycles. For recovering incomplete recent GFS runs, `GFS_CATCHUP_CYCLES=8` is a reasonable initial setting if storage and run duration permit. GFS forecast fields remain three-hourly through F240. The regular-model lookahead and the separate sounding-profile batch settings are unchanged; these workflows have different source availability and retention behavior.

**Existing installations:** `converter_config.sh` is intentionally not included in the ZIP. Any previously configured nonzero `GFS_MAX_HOUR_JOBS` and `INCREMENTAL_MAX_HOUR_JOBS` values will continue to cap tasks until explicitly changed to `0`.

### GFS performance monitoring and deployment contents

The GFS cron now starts the same low-overhead process-tree performance sampler used by the other scheduled pipelines, with label `gfs_incremental_cron`. It writes to `logs/performance/` by default and honors `EVU_PERF_ENABLED`, `EVU_PERF_SAMPLE_SECONDS`, `EVU_PERF_RETENTION_DAYS`, and `EVU_PERF_LOG_DIR`. Monitoring begins after the case and GFS writer locks are obtained, and ends with the poll. The GFS wrapper does **not** yet emit the same detailed per-field stage/task timings as instrumented regular-model converters; its resource and I/O summaries cover the whole poll.

`check_cron_status.sh` also lists the five most recent GFS cron logs alongside the hourly verification and 15-minute model logs. The GFS wrapper uses `GFS POLL START/END` rather than the other wrappers' invocation markers.

Deployment ZIPs omit `tests/`, cached Python files, local/site configuration, live output and log data, and `build_converter_zip.py`. Source-level tests and ZIP-building utilities remain in the development tree. Operational diagnostic, repair, migration, and backfill tools remain included for troubleshooting and maintenance.

### Multi-node cron and sounding catalog publication

The regular and GFS sounding catalog publishers now use per-invocation staging files and a remote-host publication lock. A two-host `flock` check is available in `check_shared_locks.py`; see `MULTINODE_PUBLICATION.md` before assuming filesystem cron locks work across login nodes. The installed cron schedules and configuration need no changes.

### GFS marine weather (v16/v17)

The GFS incremental converter ingests the common subset of global 0.16-degree
wave GRIB2 records from both model versions, when present. V16 is fetched by
range from the public AWS GFS-Wave GRIB2 and `.idx` objects; v17 uses the
configured local `products/wave/gridded/global.0p25` directory. The shared
fields are significant combined wave height (HTSGW), wind-wave height (WVHGT),
first swell-partition height (SWELL), primary period (PERPW), wind-wave period
(WVPER), and first swell-partition period (SWPER). Matching DIRPW/WVDIR/SWDIR
records supply directional arrows in the viewer. Maximum individual wave height
is intentionally excluded because it is not established as available in both
versions. All fields are optional and checked against each GRIB inventory.

Marine data are interpolated to the established 0.25-degree regional GFS
viewer grid. The global 0.25-degree wave source has different latitude coverage
from the atmospheric grid, so uncovered locations are left missing. Marine
completeness is independent of atmospheric-hour completion, allowing a later
poll to add wave fields without reprocessing atmospheric GRIBs. Marine scalar
fields take part in the existing v17-v16 and forecast-v16-F000 differences
when both source chunks exist; directions are not subtracted. Set
`GFS_MARINE_ENABLED=0` to disable this optional ingestion.


### Hourly RAP profile catalog compatibility (2026-11-03)
The shared sounding catalog publisher accepts RAP analysis profile stores at every UTC hour (00–23). Forecast model sounding stores retain their existing synoptic-hour validation. The same rules apply to remote catalog merge and expired-store cleanup; this corrects false publication failures for valid hourly RAP profiles without relaxing path restrictions.


### Operational RRFS/REFS early-cycle polling (November 2026)

The regular-model watcher now selects a newly initialized 00/06/12/18 UTC cycle 15 minutes after initialization (default `INCREMENTAL_CYCLE_LAG_HOURS=0.25`). It continues reading RRFS/REFS from the existing operational AWS bucket; no source URL change is required. This is a **polling start**, not a claim that forecast hours are already published. Unavailable indexes/products remain missing and are checked again by subsequent executions. The usual catchup, completion and concurrent-worker protections remain active. For an existing deployment with `INCREMENTAL_CYCLE_LAG_HOURS=0.75` explicitly set in `converter_config.sh`, change that value to `0.25`; site configuration overrides the script default. If early polls cause unhelpful request volume, increase the lag after reviewing the logs. GFS scheduling is unaffected.

### MRMS mid-level rotation-track verification

The verification ingestion workflow includes MRMS `RotationTrackML60min_00.50` (3–6 km AGL), sampled for the hourly valid time, converted to its own `analysis_mrms_rotation_ml60_YYYYMMDDHH.zarr` store and published alongside existing MRMS analyses. The viewer uses the one-hour radar-derived maximum for 1-hour model UH comparisons, and forms the running gridpoint maximum across all complete intervening hourly tracks for accumulated UH. Rotation-track units are **×10⁻³ s⁻¹**, not model updraft-helicity units (**m² s⁻²**). Missing hours cannot be treated as zero. The remote publication and regular retention policies handle this product.

GFS marine input selects the common global.0p25 wave fields for both v16 and v17.


### Retention-policy changes (v1.4.25)

The routine model/analysis/observation cleanup runs after 03 UTC and is normally
marked complete once per UTC day. The completion marker now includes the active
`RETENTION_DAYS` value (`YYYYMMDD:DAYS`). If retention is changed mid-day, the
next eligible model incremental poll re-runs local and remote cleanup plus catalog
refresh, rather than waiting until the following UTC day. A legacy date-only marker
triggers one reconciliation automatically. Cleanup still skips protected cases and
the analyses they require. GFS forecast-store cleanup is handled independently by
the GFS poller and must run successfully for old GFS stores to disappear.

To preview the shared model/observation remote retention using your operational
settings (from the converter directory, with the runtime environment active):

```bash
python3 multi_model_to_zarr_wcoss2.py --date "$(date -u +%Y%m%d)" --cycle 00 \
  --models all --cleanup-only --cleanup-dry-run \
  --retention-days 4 --protected-cycles-file ./protected_cycles.json \
  --output-root ./zarr --work-root ./work \
  --publish-host "$PUBLISH_HOST" --publish-dir "$PUBLISH_DIR"
```

Verify the publish host/directory variables before using this command; no files
are removed in dry-run mode. Do not remove a protected case to force cleanup.


### Independent routine data retention (v1.4.27)

Configure `converter_config.sh` with `RRFS_RETENTION_DAYS`, `GFS_RETENTION_DAYS`, and `OBS_RETENTION_DAYS`. Unset group-specific values fall back to legacy `RETENTION_DAYS`. The model incremental daily cleanup compares all three settings in its success marker so changing any one triggers another eligible sweep after 03 UTC. The GFS cron independently handles GFS model data; the shared cleanup honors the GFS setting for GFS sounding profiles. Observations/analyses include native and aligned analysis Zarr, GLM, ptype observations, SPC reports, and RAP analysis soundings. Historical protected cases remain exempt.

Suggested starting values are RRFS=4, GFS=7, OBS=7 days, but use values appropriate for disk and verification needs. Model and observation retention are based on UTC initialization/valid times, not file modification time. Existing config files are not overwritten by the distributed example.


### GFS 25-hPa native sounding-level coverage (v1.4.30)

The GFS native sounding collector can merge `pgrb2` with `pgrb2b` (or the optional `pres_a` / `pres_b` local pair) without altering map-field data. The supplemental 25-hPa moisture fields are RH, not SPFH; dewpoint is derived from colocated native T and RH. The `--inventory-only` mode now reports combined coverage and native dewpoint-capable levels. See the changelog. Completed profile hours require a forced rerun to incorporate added levels.


### GFS observed precipitation source priority (v1.4.31)

CCPA retrieval uses the 6-hour publication grouping rather than valid-hour directories and retains a separate late-arrival retry window, capped by the observation retention policy. New GFS-aligned 0.25-degree Stage IV/MRMS one-hour QPE stores are published alongside their existing regional stores. Viewer v1.1.78 selects CCPA when an entire requested accumulation period is present, then Stage IV, then MRMS. Publication and missing-data coverage must be confirmed with operational GRIB2 samples after deployment. Use the standard verification cron and inspect `catalog_analysis.json` for `gfs_ccpa_*`, `gfs_stageiv_apcp`, and `gfs_mrms_apcp` datasets.
