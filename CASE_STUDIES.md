# OMD EVU — Case-study creation and preservation

The `create_case.py` utility coordinates forecast-model conversion, native verification analyses, aligned analysis fields, and Synoptic METAR/mPING precipitation-type reports over the full UTC valid-time span of one or more model cycles. It uses the **existing** converter, Zarr v2 output format, SSH/SCP publication, and operational retention policy. No new Python packages or cron entries are required.

## Quick start

Run from the installed processing server processing directory with the operational Python/wgrib2 environment loaded and both cron jobs idle. These commands **preview** the case without writing anything:

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
  --execute --rights-confirmed --workers 12 --aligned-workers 2
```

For a local-only test, use `--execute --no-publish` (rights confirmation is needed only when publishing Synoptic/mPING reports). To explicitly reacquire and overwrite all existing model hours and analysis products, specify `--mode all`; the safer default, `--mode missing`, preserves already populated fields and uses the manual case-specific missing-field retry option to check all configured forecast hours without waiting for the scheduled retry cooldown. `--mode all` can replace valid existing data with missing fields if original source files are no longer available, so backups are recommended.

## Required configuration

- The existing operational `protected_cycles.json` must already exist, be readable, and pass `retention_policy.load_policy`. The file is **never** included in the package and is never replaced with an example during installation.
- The installed converter's Python 3.10+ runtime and `wgrib2` must already work on processing server. If needed, pass `--wgrib2 /absolute/path/to/wgrib2` and/or `--python /absolute/path/to/python3`.
- Observation retrieval requires a private `ptype_obs_secrets.sh` alongside the scripts, containing `SYNOPTIC_API_TOKEN` and `MPING_API_KEY`, with file mode `600`. No credentials are logged or distributed. `--skip-observations` runs only forecasts and analyses if observation credentials are not configured.
- Synoptic/mPING publication requires `--rights-confirmed`, reflecting the data-provider publication permissions. Without this flag, a publishing execution is refused before modifying the retention file.
- All model and verification cron processes must be idle when starting a case. The utility acquires both `.model_incremental.lock` and `.multisource_cron.lock` without waiting; if either is busy, the invocation aborts without processing. To avoid skipped scheduled runs, pause the two cron entries during a long backfill and restore them afterward.

## Data and time selection

Forecast cycles must be valid UTC initialization hours (`00`, `06`, `12`, or `18`). One-cycle selection uses `--cycle YYYYMMDDHH`; a range uses `--start-cycle` and `--end-cycle` in the same format. Ranges are inclusive and support up to 124 cycles per invocation; small batches (one to four cycles) are preferable for initial validation and operational load control.

By default, all eight model datasets are selected: RRFS, HRRR, REFS Mean, and REFS m001–m005. Set `--models rrfs,hrrr` (or another comma-separated subset) to restrict models. Individual models retain their native maximum forecast-hour limits (RRFS F084, REFS Mean/members F060, HRRR F048; REFS Mean starts at F001).

The case verification window extends from the earliest selected initialization time through **F084 of the latest selected initialization**. It covers all shorter-range models and permits full comparisons, not merely the time of initialization. Native verification processing includes RAP, RTMA/URMA, MRMS, and scheduled NOHRSC products with their existing data-latency and field-availability rules. The alignment pass regrids available verification fields onto the forecast grid. Historical verification uses exact UTC `--start-hour` / `--end-hour` windows, split into at most 168 consecutive hours per command. Source datasets unavailable at their original archives cannot be fabricated.

Synoptic/mPING collection uses the same valid-time coverage, divided into at most 168-hour API windows. It writes one sanitized hourly observation GeoJSON file per valid hour with the existing ±30-minute matching window, then publishes it when requested and authorized. Observation hours whose ±30-minute window is not yet complete are deferred. The script must be rerun later for future valid times, delayed reports, or verification fields not yet available. Repeating the same `--execute` invocation is safe in `--mode missing` and updates the same protected case.

## Protected-cycle updates

With `--execute`, the utility validates `protected_cycles.json`, takes both operational locks, then **atomically merges** selected cycles into `protected_cycles`. It retains previously protected cycles and every `protected_analysis` entry, and merges model subsets without reducing any existing protection. Each updated operational list has a timestamped mode-0600 backup alongside it (`protected_cycles.json.case-backup-*`). The list remains mode 0600 and is excluded from Git. This protection is written **before** downloads/publishing so interrupted case studies remain safe from retention cleanup.

The existing retention policy automatically protects selected model stores and matching verification/analysis stores from initialization through F084, including when an older case falls outside the normal 7-day window. Observation GeoJSON files currently have no automatic expiration in the observation collector; they should be kept with the archived case. Protection cannot restore source data that had already been deleted before the cycle was selected.

## Publishing, recovery and status

The utility uses non-overwriting model conversion by default (`--incremental --force-missing-check`), then runs a `--publish-only` step for **every selected cycle** so already complete stores and catalogs are also published. It subsequently fetches and publishes verification products, aligns and publishes analysis, and retrieves/publishes observations. `--no-publish` affects all three publication paths. Every phase logs its command (without provider credentials), and a nonzero subprocess status is retained in the final error summary; independent subsequent phases continue so available partial case data can still be published.

The tool exits nonzero if any attempted step reports a failure. It may still exit zero while some sources/forecast hours are legitimately not yet available, so the model catalogs, analysis catalogs, and hourly observation file counts should be checked for case completeness. Re-running the same case is the supported recovery mechanism. Do not edit model Zarr stores or delete `.published/` snapshots to trigger recovery; `--mode all` is unnecessary for normal retries.

### Optional flags

| Flag | Behavior |
|---|---|
| `--mode missing` | Default; process missing forecast hours/fields, leaving existing chunks intact |
| `--mode all` | Explicitly overwrite existing forecast/analysis data when source files remain available |
| `--no-publish` | Retain output locally; no remote SCP/SSH publication |
| `--skip-analysis` | Omit native and aligned verification |
| `--skip-observations` | Omit Synoptic and mPING collection |
| `--workers N` | Number of concurrent forecast-hour workers, 1–20 (default 12) |
| `--aligned-workers N` | Independent regridding processes, 1–4 (default 2) |
| `--models ...` | Restrict case-study model IDs; retained models are merged into existing protection |
| `--rights-confirmed` | Required when executing with authorized Synoptic/mPING publication |

The case utility does not modify either cron entry, the viewer, published file formats, hourly collection settings, or existing operational paths.
