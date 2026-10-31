# OMD EVU Converter

**Current release: v1.3.6** · See [release notes](RELEASE_1_3_6.md) and the [changelog](CHANGELOG.md).

The converter downloads supported forecast and verification products, extracts requested GRIB2 fields, writes browser-readable Zarr v2 datasets and JSON catalogs, and publishes data to a configurable web server for the companion model viewer. It runs on a processing server with Python 3.10+, `wgrib2`, and the site-provided runtime environment. No source data, credentials, or local deployment configuration are included in the archive.

Model guidance includes RRFS, HRRR, REFS members and the operational REFS Mean. Available verification datasets include RAP, RTMA, URMA, MRMS, NOHRSC and observational feeds. Native and aligned analysis stores serve different purposes; an available local dataset must still be published before it can appear in the web viewer. Optional fields are stored only when supported by the upstream source inventory.

The package includes an incremental scheduler, bounded parallel model conversion and analysis alignment, historical case creation, protected-case retention, sounding profile collection, audit utilities, and controlled publication. Default unprotected retention is **seven days**; protected cases are excluded from routine cleanup. Saved cases are maintained separately from real-time collection.

## Documentation

- [Current release notes](RELEASE_1_3_6.md) — worker defaults, performance tuning, compatibility, and checks
- [Case studies](CASE_STUDIES.md) — protected historical case processing
- [Sounding profiles](SOUNDING_PROFILES.md) — pressure-level collection and diagnostics
- [RAP field audit](RAP_FIELD_AUDIT.md) — upper-air and CAPE inventory checks
- [Shear repair](SHEAR_REPAIR.md) — targeted derived-field maintenance
- [Changelog](CHANGELOG.md) — historical changes (without separate outdated release files)

## Upper-air, shear, and retention capabilities

- Adds **300-hPa U/V winds, geopotential height, and derived wind speed** for forecast models, and RAP F000 300-hPa wind/height fields, including aligned analysis handling. Existing completed cycles can be backfilled with `--repair-upper-air` and `--publish-only`.
- Corrects a bulk-shear conversion pitfall: RRFS/REFS native GRIB VUCSH/VVCSH components may be wind differences (m/s), whereas HRRR/RAP may supply vertical gradients (s^-1). Unit-aware derivation inspects magnitudes and source conventions and rejects implausible speeds. Already-written shear can be recomputed using `repair_bulk_shear.py` without any network downloads. See [SHEAR_REPAIR.md](SHEAR_REPAIR.md).
- Includes `audit_severe_fields.py` for inventory-only checks of native `SCCP`, `STPC`, and `SIGT`. These diagnostic products remain unavailable when the actual source GRIB products do not contain the records; no substitute diagnostic is invented.
- Applies **seven-day retention** to dated incremental and hourly logs, rotated legacy cron stderr logs, observation GeoJSON files, and expired per-store publish snapshots on the processing server. Remote cleanup additionally removes expired observation GeoJSON, abandoned model-transfer archives, and obsolete staging directories. Valid hours from protected model cycles and protected analysis ranges remain pinned. Static code, configuration, catalogs, current jobs, locks, and credentials are never swept. Standard native/remote analysis retention remains in its existing workflow.
- The existing `run_model_incremental_cron.sh` daily 03 UTC cleanup now passes its logs directory to the retention routine. No cron entry changes are needed.

### NOHRSC snowfall metadata handling

NOHRSC 6-/24-hour snowfall conversion now reads native GRIB variable units from the bracketed descriptions returned by `wgrib2 -V` (for example `[m]`, `[mm]`, `[cm]`, or `[in]`). Previously, the converter expected a nonexistent `units=` key and rejected otherwise readable NOHRSC products. Unsupported/unknown physical units remain rejected; input depth units are converted to meters before writing the viewer-compatible Zarr arrays. Failed earlier hours can be retried with the normal verification backfill/cron process; existing model stores and scheduled cron entries are unchanged.

### Case-study creation

The new `create_case.py` coordinates a single initialization cycle or inclusive six-hour cycle range, restores missing or explicitly overwritten model fields, retrieves native verification/analysis products and aligned analyses for the case valid-time window, and collects time-matched Synoptic METAR/mPING reports. It atomically merges the selected cycles into the operational `protected_cycles.json` (with timestamped backups) before processing, preserving existing case entries. Dry-run preview is the default; `--execute` performs the work, and `--rights-confirmed` is required for authorized observation publication. The utility uses both existing cron locks and does not require any additional cron entry. See [CASE_STUDIES.md](CASE_STUDIES.md) for complete commands, limitations, and recovery.

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

The source inventory determines actual field availability. Severe-weather fields use native GRIB2 diagnostics where present: `VUCSH`/`VVCSH` components for bulk shear, `HLCY` for SRH, `MXUPHL` for hourly UH maxima, and native SCCP/STPC/SIGT parameter names for SCP and distinct STP formulations. Accumulated UH maximum is the maximum of validated hourly maxima, not an additive accumulation. An unavailable GRIB record remains unavailable.

## Main workflow

- **Two coordinated cron entries** are used in incremental mode: the original hourly wrapper continues RAP/RTMA/URMA/MRMS, aligned analyses, and authorized Synoptic/mPING observations; `run_model_incremental_cron.sh` polls model sources every 15 minutes. The optional `incremental_model_config.sh` sets `MODEL_SCHEDULER=incremental`, disabling old 04/10/16/22 UTC model work in the hourly wrapper.
- The new model watcher starts checking a cycle approximately **45 minutes after initialization** (configurable), ingests only discovered forecast hours and missing fields, and revisits the prior three cycles. Each pass has a configurable forecast-hour frontier and job budget.
- Completed fields are never overwritten during incremental recovery. The coarse `processed` marker is supplemented by on-disk chunk checks, staged interval awareness, and a per-hour missing-field retry clock (default 60 minutes). Native product gaps may remain missing, with no invented values.
- **First publish is a full bootstrap; subsequent publishes send changed or removed Zarr files plus a catalog.** A successful-transfer snapshot is retained under `work/.published/`; unsuccessful transfers do not advance the snapshot. Changed files are installed atomically and the catalog is published last. `--publish-mode full --publish-only` forces a fresh full-store upload if necessary.
- REFS Mean's `mean` product supplies 500-hPa U/V wind and height but not native 500-hPa `ABSV`. Missing `absv500` is derived from the ensemble-mean winds on the model Lambert grid, adding the spherical metric and Coriolis terms. The Zarr metadata explicitly identifies **DERIVED** provenance; this is a derived physical quantity, not a substituted native GRIB record.
- Established delayed REFS Mean repair continues at 72/96 hours. The model watcher also performs daily retention; protected case studies remain exempt.
- Historical **legacy mode** (one hourly cron and 04/10/16/22 UTC model ingestion) remains available by omitting `incremental_model_config.sh` and the new watcher.
- Verification uses the existing default **20-hour lookback** (`VERIFY_LOOKBACK_HOURS=20`) for delayed URMA and other products. NOHRSC has a separate configurable lookback; only native 6-/24-hour snowfall is downloaded.
- Regular data use **7-day retention** locally and remotely. An operational `protected_cycles.json` maintains explicitly selected cases and their verifying analyses beyond the regular retention period.
- `aligned_analysis.py` remaps available native verification stores to the model grid for efficient model−analysis differences; outputs are published with a separate aligned-analysis catalog.
- Native NOHRSC 48-/72-hour downloads are disabled. The viewer derives 48-hour snowfall from two aligned, complete NOHRSC 24-hour analyses. MRMS 3/6/24/48-hour precipitation analysis is derived from complete hourly records.

## Included source files

| File | Role |
| --- | --- |
| `multi_model_to_zarr_wcoss2.py` | Main forecast conversion, repair, verification dispatch, catalogs and publishing |
| `run_multimodel_cron.sh` | Hourly RAP/verification/observations launcher; legacy model fallback |
| `run_model_incremental_cron.sh`, `incremental_model_config.example.sh` | 15-minute model watcher and activation template |
| `capture_converter_runtime.sh`, `converter_cron_env.sh` | Captured processing server runtime support |
| `verification_pipeline.py`, `verification_ingest.py`, `verification_convert.py`, `verification_sources.json` | Analysis ingest, conversion, sources and catalog publishing |
| `aligned_analysis.py` | Remapping native analyses to the common model grid |
| `retention_policy.py`, `protected_cycles.example.json` | Shared model/analysis retention policy and template |
| `backfill_model_history.py` | Bounded historical cycle backfill helper |
| `repair_refs_mean_archive.py`, `repair_analysis_access.py` | Targeted maintenance utilities |
| `audit_model_retention.py`, `audit_analysis_alignment.py` | Read-only inspection and auditing |
| `ptype_observations.py` | Optional credentialed METAR/mPING collection, classification and authorized GeoJSON publishing |
| `create_case.py`, `CASE_STUDIES.md` | Safe case-study backfill, publication, and protected-cycle management |
| `tests/` | Targeted converter regression tests |

## Deployment on a processing server

All Python source modules and shell wrappers belong together in the established processing directory. Existing `zarr/`, `work/`, `logs/`, SSH configuration, local runtime files and operational `protected_cycles.json` must be **preserved** when upgrading. Site-specific paths and publishing settings in the scripts must be checked before use at a different installation.

A known-working interactive shell can capture the converter runtime:

```bash
cd /path/to/zarr_processing
chmod 750 run_multimodel_cron.sh run_model_incremental_cron.sh capture_converter_runtime.sh
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

### Activate the incremental scheduler

After all currently running manual converters or legacy cron jobs have finished, copy the shipped template to the local, untracked configuration file:

```bash
cp incremental_model_config.example.sh incremental_model_config.sh
chmod 600 incremental_model_config.sh
chmod 750 run_model_incremental_cron.sh
```

**Both** entries below must be installed in `crontab -e` (substitute the actual installation path). The hourly entry is unchanged; it now handles only analyses and observations while incremental mode is enabled. The 15-minute watcher handles forecast models and their catalogs:

```cron
10 * * * * /path/to/zarr_processing/run_multimodel_cron.sh >> /path/to/zarr_processing/cron_errors.log 2>&1
*/15 * * * * /path/to/zarr_processing/run_model_incremental_cron.sh >> /path/to/zarr_processing/model_cron_errors.log 2>&1
```

The watcher uses a dedicated `.model_incremental.lock`, so model conversion cannot overlap another model poll; the hourly wrapper retains `.multisource_cron.lock` for analyses/observations. These workflows write different stores and may run concurrently. Manual model conversions must be coordinated with the **model watcher lock**; avoid deploying or performing full backfills during active model writes. A cycle can become visible progressively as forecast-hour GRIBs appear.

Default tuning: `INCREMENTAL_CYCLE_LAG_HOURS=0.75`, `INCREMENTAL_CATCHUP_CYCLES=3`, `INCREMENTAL_LOOKAHEAD=12`, `INCREMENTAL_MAX_HOUR_JOBS=48`, `INCREMENTAL_MISSING_RETRY_MINUTES=60` and `INCREMENTAL_WORKERS=6`. Optional `NO_PUBLISH=1` suppresses remote transfers. Polls are skipped when the previous poll remains active. `logs/model_incremental_YYYYMMDD_HHMMSS.log` records actual progress.

An existing `converter_cron_env.sh` (generated by the established runtime capture script) is still required. No new Python modules, package installs, or changes to the viewer's Zarr reader are needed.

**First deployment:** The first successful model publish for each store performs a full-store transfer to establish the persistent publication snapshot; subsequent runs transfer only changed files. A forced `--publish-mode full` can re-seed an existing web server store. The automatic 72/96-hour REFS Mean repair and daily model retention remain active. The hourly verification and credentialed observation collection continue unchanged.

### Selected configuration

`RETENTION_DAYS=14`, `CATCHUP_CYCLES=3`, `VERIFY_LOOKBACK_HOURS=20`, `CYCLE_LAG_HOURS=3.5`, `WORKERS=8`, `NO_PUBLISH=1` (local-only tests), `PROTECTED_CYCLES_FILE`, `PUBLISH_HOST`, `PUBLISH_DIR`, `ALIGNED_ENABLED=1` and `ALIGNED_MAX_NEW_HOURS` are supported by the existing wrapper. The converter and verification source settings contain further options.

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
- Optional local settings can be placed in `ptype_obs_config.sh` (also excluded from repositories/releases). For routine collection, set `export PTYPE_OBS_ENABLED=1`. To publish to web server, `export PTYPE_OBS_PUBLISH=1` and `export PTYPE_OBS_RIGHTS_CONFIRMED=1` are *both* required. These switches are intentionally **off by default**, pending confirmation of Synoptic's map-display license/attribution and applicable mPING API terms.
- The hourly cron launcher loads the private configuration file after the proven processing server runtime. The authorized observation collection remains hourly and is independent of the 15-minute model watcher. If credentials are not installed, observation collection is skipped.

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

RAP analysis ingestion extracts sea-level `MSLMA` from the existing `awp130bgrbf00` source and pressure-level temperature, height and U/V wind components from the existing `awp130pgrbf00` source. RAP F000 also supplies 500-hPa absolute vorticity, 0–6 km shear (`VUCSH`/`VVCSH` vector magnitude times 6000 m), 0–1 and 0–3 km storm-relative helicity, surface CAPE, lowest-90-hPa mixed-layer CAPE and lowest-180-hPa most-unstable CAPE. RAP analysis is model-based, not independent observation. Fields not found in the exact operational F000 inventory are not fabricated. The `awp130pgrbf00` inventory does not supply 0–1 km shear or the requested maximum UH/SCP/STP records; no RAP analysis is advertised for these fields.

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

# Multiple initialization cycles (inclusive 6-hour sequence)
python3 create_case.py --start-cycle 2026102800 --end-cycle 2026102918 \
  --execute --rights-confirmed
```

The case tool adds the model cycles to the existing operational protection list before processing, backs up the list, uses exact historical verification/observation windows, and retains the normal publication/catalog logic. Runs for recent cases must be repeated after future verifying hours become available. See `CASE_STUDIES.md` for site setup, lock requirements, missing-only versus full reprocessing, and explicit local-only operation.


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

The 03 UTC incremental cron maintenance performs the real cleanup automatically. Legacy `cron_errors.log` and `model_cron_errors.log` stdout/stderr captures are rotated to dated files in `logs/` if nonempty; those dated logs are then expired after 7 days. The script never deletes arbitrary files or directories just because they are old. Source trees, environment files, access keys, protected cases, catalogs, and lock files are not retention targets.

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
