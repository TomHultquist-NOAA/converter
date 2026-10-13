# OMD EVU Unified Model and Verification Converter

**Version 6.6 — WCOSS2 operational source**

Downloads operational forecast and verification GRIB2 products, converts selected fields to browser-readable Zarr v2, generates catalogs, and publishes the results to the OMD EVU Model Viewer web server. The pipeline uses Python 3.10+ (standard library only) and an existing `wgrib2` executable; it does not require installing packages on WCOSS2.

**One hourly cron job** handles both workflows: RAP, RTMA, URMA, and MRMS are checked every hour, and model cycles are processed at **04, 10, 16, and 22 UTC**. Local and published data are subject to a configurable **14-day retention** policy for regular data, plus an optional indefinite whitelist for model cycles and hourly verification/analysis sources. A shared `flock` lock prevents concurrent cron runs.

## Repository contents

| File | Role | Needed? |
| --- | --- | --- |
| `multi_model_to_zarr_wcoss2.py` | Main model and verification entry point; catalogs, publishing, retention | **Required** |
| `run_multimodel_cron.sh` | Unified hourly cron launcher, locking, model-cycle selection | **Required for cron** |
| `converter_cron_env.sh` | Loads a captured WCOSS2 runtime in cron | **Required for cron** |
| `capture_converter_runtime.sh` | Creates the local runtime configuration from a working interactive shell | **Required for initial setup / changes** |
| `verification_pipeline.py` | Hourly verification orchestration and publishing decisions | **Required** |
| `verification_ingest.py` | Download and extraction of RAP, RTMA, URMA, MRMS GRIB2 | **Required** |
| `verification_convert.py` | Zarr conversion and derived verification fields | **Required** |
| `verification_sources.json` | Analysis field mappings and latency settings | **Required** |
| `protected_cycles.example.json` | Template for the operational model-cycle and analysis protection whitelist | **Required for initial setup** |
| `retention_policy.py` | Shared safe whitelist parser and model/analysis hour-range checks | **Required** |
| `repair_analysis_access.py` | Optional, read-only-by-default fix for previously published Analysis Zarr permissions | **Maintenance utility** |
| `.gitignore` | Excludes generated data, runtime paths and operational files from Git | **Recommended** |
| `README.md` | Setup and operations documentation | **Recommended** |

Keep these files together in the processing directory so Python can import the verification helper modules. The permissions-repair utility is not run by cron and can be left out of a minimal **deployment**, but is useful to keep in the source repository for future recovery.

## Models and verification sources

| Dataset | Available forecast / analysis hours |
| --- | --- |
| RRFS | F000–F084 |
| REFS mean | F001–F060; categorical weighted-mean rain/freezing rain/ice pellets/snow, REFC from PMMN, interval-aware WEASD and ASNOW |
| REFS members m001–m005 | F000–F060 |
| HRRR | F000–F048 |
| MRMS | Hourly verification: composite reflectivity, precipitation rate, one-hour QPE |
| URMA | Preferred hourly verification: 2-m temperature/dewpoint, 10-m wind/gust, derived 2-m RH |
| RTMA | Preliminary fallback for the same five fields while URMA is delayed |
| RAP | Hourly F000 analysis: snow depth, SWE, 925-/850-/700-hPa temperature, derived snow-presence mask |

REFS mean categorical fields are **ensemble-weighted means**, not deterministic category flags. The unidentified local parameter (GRIB discipline 0/category 6/parameter 202) is not yet mapped as PTYPE. REFS mean reflectivity comes from the `pmmn` product; the other new fields come from `mean`. Both WEASD and ASNOW carry accumulation intervals, which are converted into run totals only when the necessary earlier-hour totals exist.

MRMS conversion uses **native stride 1** (no spatial decimation). RAP snow presence is a binary proxy derived from depth, *not* fractional snow-cover percentage. Verification is indexed by **valid UTC hour**, with source-specific publication delays set in `verification_sources.json`. For temperature/dewpoint/RH and 10-m wind/gust, the viewer **always prefers URMA to RTMA for an identical valid time**. RTMA is labeled preliminary. Both sources may coexist in the 14-day archive; the viewer makes the replacement automatically by precedence rather than rewriting Zarr stores. Data not yet published by a source are retried during subsequent hourly runs; no older hour is substituted.

Output includes model-specific catalogs (`catalog_rrfs.json`, `catalog_refs_m001.json`, etc.) and `catalog_analysis.json` for the viewer's Analysis panel. The existing top-level `catalog.json` is not overwritten by the model publisher.

## Initial setup on WCOSS2

Install or update the source files in the existing `zarr_processing` directory. Preserve your existing `zarr/`, `work/`, `logs/`, SSH setup, and any local configuration. The included cron wrapper has **site-specific directory and RZDM destination defaults**; review them when deploying anywhere other than the established WCOSS2 installation.

From an **interactive shell with a known-working Python and wgrib2 environment**:

```bash
cd /path/to/zarr_processing
chmod 750 run_multimodel_cron.sh capture_converter_runtime.sh
# One-time setup only: DO NOT overwrite an existing protected_cycles.json
if [ ! -e protected_cycles.json ]; then
  cp protected_cycles.example.json protected_cycles.json
fi
./capture_converter_runtime.sh
```

This writes `converter_runtime_paths.sh` locally. **Do not commit or share it.** It captures executable/library paths for cron; the cron wrapper does not need to load WCOSS2 modules or source `.bashrc`. A clean `wgrib2 -version` response is accepted even if that particular command returns a nonzero status (as observed with WCOSS2 3.8.0).

Test the environment without running any processing:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin CHECK_ENV_ONLY=1 \
  /bin/bash ./run_multimodel_cron.sh
```

Expected: `Cron runtime OK: Python=... ; wgrib2=...`.

## Scheduling

Install **one** cron entry and remove any earlier four-times-daily entries for the same converter:

```cron
10 * * * * /path/to/zarr_processing/run_multimodel_cron.sh >> /path/to/zarr_processing/cron_errors.log 2>&1
```

The sample cron runs at **HH+10 UTC**, as in the current deployment. Its verification pass checks current-hour MRMS reflectivity/rate (initial 10-minute delay) and previous-hour RAP F000 and RTMA (65/60-minute delays). MRMS one-hour QPE is also attempted at HH+10 but may arrive later; missing files are retried hourly. URMA is checked after 450 minutes. All unavailable products are retried during subsequent hourly runs. Model production remains at UTC hours 04, 10, 16, 22 even when the cron entry itself fires ten minutes after each hour. The wrapper handles verification first. At 04/10/16/22 UTC it processes the newest eligible model initialization cycle and, by default, the preceding cycle (`CATCHUP_CYCLES=1`). An invocation that encounters an active shared lock skips rather than overlapping work. A full 14-day automatic model backfill is **not** implemented; recover older missed cycles explicitly as needed.

Common optional settings: `WORKERS=8`, `RETENTION_DAYS=14`, `PROTECTED_CYCLES_FILE=/path/to/protected_cycles.json`, `CYCLE_LAG_HOURS=3.5`, `CATCHUP_CYCLES=1`, `VERIFY_LOOKBACK_HOURS=20`, `MRMS_STRIDE=1`, `MODELS=all`, `NO_PUBLISH=1` (test), `PUBLISH_HOST`, and `PUBLISH_DIR`.

## Retention: 14 days and permanent model/analysis protection

**Every model and verification/analysis source uses the same 14-day default** on WCOSS2 and RZDM. Model age is measured from UTC cycle initialization, and Analysis age is measured from UTC valid hour. The cron wrapper passes `RETENTION_DAYS=14` and **the same whitelist file** to both model and verification workflows. The standalone `verification_ingest.py` and `verification_convert.py` tools also validate the whitelist before cleanup. If an existing cron environment overrides `RETENTION_DAYS=7`, remove that override or change it to 14.

Edit the operational `protected_cycles.json` to keep selected cases **indefinitely**. It is deliberately excluded from GitHub and the release ZIP. Your existing v6.5 file remains valid without edits (its schema identifier stays `omd-evu-protected-cycles-1`). To start a new file, use `cp -n protected_cycles.example.json protected_cycles.json`. Never overwrite a populated operational whitelist when installing updates.

The following example shows **three independent ways** to protect cases:

```json
{
  "schema": "omd-evu-protected-cycles-1",
  "protected_cycles": [
    "20261012_18",
    {
      "date": "20261010",
      "cycle": "06",
      "models": ["refs_mean", "refs_m001"],
      "analysis_through_fhr": 60,
      "analysis_sources": ["mrms", "rap", "urma", "rtma"]
    }
  ],
  "protected_analysis": [
    "2026100912",
    {
      "start": "2026100800",
      "end": "2026100818",
      "sources": ["mrms", "urma"]
    }
  ]
}
```

**What this protects:**

- `"20261012_18"` preserves **all eight forecast model stores** initialized at October 12, 18 UTC. Existing string entries remain **model-only** to avoid unexpectedly retaining large amounts of MRMS data.
- The October 10, 06 UTC object preserves the specified REFS models **and all selected verification products valid F000–F060** (October 10 06 UTC through October 12 18 UTC, inclusive). Set `analysis_through_fhr` to the desired forecast lead, e.g. `84`; omit `analysis_sources` to protect all four verification source families.
- `"2026100912"` preserves **all analysis products** valid at that one UTC hour, independent of any model cycle. The `start`/`end` object preserves only MRMS and URMA over an inclusive UTC valid-hour range. Set `sources` to `["all"]` or omit it to preserve every analysis product.

`protected_analysis` is optional. Accepted analysis source groups are **`mrms`** (reflectivity, precipitation rate, one-hour QPE), **`rap`** (surface and pressure analyses), **`rtma`**, and **`urma`**. Individual product names such as `mrms_apcp` or `rap_pressure` are also accepted. Times are UTC `YYYYMMDDHH`, while cycles are `YYYYMMDD_CC` (`CC` = 00, 06, 12, 18). `through_fhr` and `analysis_through_fhr` allow an inclusive 0–744-hour lead; explicit start/end ranges can cover up to 366 days.

Protected **model and analysis Zarr stores and their catalog entries** are retained on both hosts. Model selections, archives and dated work-cycle directories are also exempt; protected analysis staging manifests/remaining GRIBs are retained by hour. Routine logs are still removed after 14 days. **Protected analyses do not expire merely because URMA becomes available**; RTMA and URMA are distinct data sources, with the viewer preferring URMA at the same valid time.

**Fail-safe behavior:** The configuration is checked at the beginning of **every** unified hourly verification and model invocation. Missing, invalid, or symlinked whitelists prevent processing/cleanup instead of silently treating everything as unprotected. RZDM receives validated analysis protection rules and uses them for remote cleanup. Removing an entry makes its data eligible for expiry at the next cleanup. Already-deleted data cannot be restored by adding a whitelist entry; protect it before it expires. Protecting long periods of native-resolution MRMS data can consume substantial disk space.

**Preview model cleanup** after maintenance (only when cron/other converters are stopped):

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261013 --cycle 00 --cleanup-only --cleanup-dry-run \
  --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --protected-cycles-file "$PWD/protected_cycles.json"
```

This previews **model** cleanup on WCOSS2 and RZDM, without deleting anything; the analysis cleanup runs separately in the regular hourly verification workflow. To inspect the analysis protection rules safely without running downloads or deletions:

```bash
python3 - <<'PY'
from retention_policy import load_policy
p = load_policy('protected_cycles.json')
print('Protected model stores:', len(p.model_stores))
for start, end, products in p.analysis_rules:
    print('Protected Analysis UTC:', start, 'through', end, ','.join(products))
PY
```

Both production paths use your updated rules on the next cron invocation, without any viewer changes.

## Verification latency and source precedence

Configured initial availability delays (minutes after the analysis valid hour):

| Source/product | Delay |
| --- | ---: |
| MRMS composite reflectivity and precipitation rate | 10 |
| MRMS 1-hour QPE | 10 (availability must be tested) |
| RAP F000 | 65 |
| RTMA | 60 |
| URMA | 450 |

These values are **initial eligibility thresholds**, not publication guarantees. The hourly job retries sources that have not arrived. Check `verification_sources.json` when actual latency differs. At HH+10, the current hour is eligible only for the near-real-time MRMS products; the previous hour is eligible for RAP and RTMA. RTMA takes over only for exact-hour URMA fields that have not yet been published.

## REFS mean backfill

The regular processing path now downloads `mean` and `pmmn` and ingests the new fields for new cycles. Repair already completed local REFS mean stores without overwriting existing chunks:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261013 --cycle 00 --models refs_mean --hours 1-8 \
  --repair-refs-mean --workers 2 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --no-publish
```

Inspect the catalog and store, then use `--publish-only` for the same date/cycle to upload the repaired store and catalog. Omit `--hours` to check F001–F060; `--repair-refs-mean` skips fields and hours already present. Do not overlap a manual repair with cron: use the same `.multisource_cron.lock` lock or temporarily disable the cron job.

Note: a record such as `3-4 hour acc` is an **interval** amount. WEASD/ASNOW can be shown as run-total accumulations only if all prior necessary intervals exist. A missing total must remain unavailable, not be mislabeled as cumulative.

## First RTMA validation

Before installing the new converter into live cron, test a short period in your working WCOSS2 shell:

```bash
python3 multi_model_to_zarr_wcoss2.py --verification-only \
  --source rtma --lookback-hours 4 --no-publish \
  --output-root "$PWD/zarr" --work-root "$PWD/work"
```

After a successful local conversion, re-run without `--no-publish` and test the viewer for a valid time when URMA is not yet due; then confirm that a later URMA file takes precedence. Test mean F004 and PMMN F008 inventory selection using `--inventory-only` if needed. Both tests depend on WCOSS2's live public S3 access.

## Manual processing and recovery

Run from the `zarr_processing` directory in your working WCOSS2 environment. Set `OMP_NUM_THREADS=1` and `OMP_THREAD_LIMIT=1` to avoid excessive OpenMP threading in wgrib2.

**Verification download, conversion, and publishing (all sources):**

```bash
python3 multi_model_to_zarr_wcoss2.py --verification-only \
  --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --lookback-hours 20 --mrms-stride 1
```

Add `--no-publish` to test locally without SCP transfer.

**One model cycle (all models):**

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date YYYYMMDD --cycle 18 --models all --workers 4 \
  --output-root "$PWD/zarr" --work-root "$PWD/work"
```

**Repair previously processed REFS member pressure-level temperatures:**

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date YYYYMMDD --cycle 12 \
  --models refs_m001,refs_m002,refs_m003,refs_m004,refs_m005 \
  --repair-refs-pressure --workers 4 \
  --output-root "$PWD/zarr" --work-root "$PWD/work"
```

REFS pressure-level temperature uses the separate `prslevnomads` product. The repair adds missing `t925`, `t850`, and `t700` fields to previously processed member hours without overwriting unrelated fields. Omit `--no-publish` to transfer the repaired stores. Do not run a manual backfill simultaneously with cron; coordinate via the shared lock or temporarily disable cron.

**Retry publication of an already repaired model cycle:**

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date YYYYMMDD --cycle 12 --models refs_m001 \
  --publish-only --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --no-remote-cleanup --no-local-cleanup
```

Use this after an intermittent SCP/SSH publishing failure (including SSH exit status 255) rather than repeating downloads. Publication of a model store may retransmit its full Zarr archive.

## Logs, access, and retention

- Timestamped cron logs: `logs/multisource_YYYYMMDD_HHMMSS.log`; startup errors: `cron_errors.log`.
- Verification datasets and catalogs publish beneath the configured RZDM `data/analysis/` tree and `data/catalog_analysis.json`.
- The publisher sets web-readable permissions for new Analysis stores. If older stores return **HTTP 403**, run `python3 repair_analysis_access.py` to inspect, then `python3 repair_analysis_access.py --apply` only after checking the target host/path. This maintenance utility does not run automatically.
- Local analysis publishing state (`zarr/.analysis_publish_state.json`) is deliberately excluded from Git. Do not delete it during normal operation.
- Regular datasets expire after **14 days** on both WCOSS2 and RZDM. Pinned model cycles and selected verification hours are exempt on both hosts. Full-resolution MRMS and an increasing number of protected cycles can substantially increase disk usage.
- Live RAP, URMA, and MRMS conversion/publishing and Analysis display have been verified; the repaired REFS member temperatures have also been displayed. The cron environment check passed, but monitor unattended cron execution separately.

## GitHub hygiene

Commit the **source files, JSON templates, `.gitignore`, and README only**. The operational `protected_cycles.json` is intentionally ignored so future deployments cannot overwrite your protected list; keep a backed-up copy. Keep generated Zarr stores, downloaded GRIB2, temporary archives, logs, processed manifests, and the captured WCOSS2 runtime outside Git. `.gitignore` protects *untracked* files; if any generated file was already committed, untrack it with `git rm --cached <path>` (do not delete your operational copy). Never commit SSH keys, tokens, or passwords.

The current scripts contain installation-specific filesystem paths, an SSH user, and a restricted-site publishing destination as operational defaults. They are **not credentials**, but they will be visible to anyone who can read the GitHub repository. Review those defaults and repository visibility before making this repository public.
