# AI / GEFS model-set converter: installation and operations

**Release:** 1.4.39. Compatible with existing GFSv16/v17, RRFS/REFS, verification analyses and protected cases. Requires the processing environment's **Python 3.10+**, working **wgrib2**, and outbound HTTPS to the model providers. No extra Python packages are needed.

## 1. Safe update

Back up your existing processing directory and **keep** the operational `converter_config.sh`, `converter_runtime_paths.sh`, `protected_cycles.json`, model data (`zarr/`), work files, logs, and SSH configuration. Extract the ZIP *over* the existing processing code. Never replace operational configuration with `converter_config.example.sh`; that is only a reference.

The distribution integrates hourly CCPA, native 3/6-hour CCPA, native 6/24-hour Stage IV and optionally extra MRMS products. These are produced and published by the **shared verification pipeline**; all model sets use the same analysis catalog. No separate AI verification archive is created. CCPA 1-hour availability is determined by actual source release, not assumed from 3-hour products.

## 2. Configure AI independently

Add the following to the existing `converter_config.sh`, adjusting retention to your existing model policy. The protected-case and publishing settings continue to use your existing shared configuration.

```bash
export AI_ENABLED=1
export AI_MODELS=all
export AI_LAG_HOURS=4.0
export AI_CATCHUP_CYCLES=3
export AI_SOURCE_MODE=local
export AI_LOCAL_WORKERS=8
export AI_REMOTE_WORKERS=3
export AI_NOMADS_FALLBACK=0
export AI_MAX_HOUR_JOBS=12
export AI_RETRY_MINUTES=45
export AI_RETENTION_DAYS=7
```

The 4-hour delay is an **initial polling eligibility setting**, not a guarantee or a fixed model latency. Missing lead times are retried on later polls. Adjust the lag based on your logs. Twelve eligible lead jobs per model and cycle per poll limits first-run I/O; set `AI_MAX_HOUR_JOBS=0` to remove that cap once a backlog is caught up and resources permit. You can temporarily use `AI_CATCHUP_CYCLES=0` for a single recent cycle; the default checks the most recent four eligible cycles. Avoid overlapping long manual backfills with case processing.

The converter stores AI grids at 0.25 degrees; GEFS ensemble **mean** uses two native-resolution stores: 0.25 degrees for available surface variables and 0.5 degrees for pressure-level variables (never downloading individual ensemble members), cropped to the same geographic bounds as existing GFS data. **Default leads are F000–F240 every six hours**; F384 can be explicitly requested for supported sources with `--hours 0-384`. No additional map-domain interpolation is performed. An AI source missing 2 m dewpoint or specific humidity remains missing rather than using a substitute pressure level.

## 3. Validate before scheduling

From the existing processing directory with the working Python/wgrib2 runtime:

```bash
python3 ai_to_zarr.py --date 20261108 --cycle 06 --models all --plan
python3 ai_to_zarr.py --date YYYYMMDD --cycle 06 --models all \
  --hours 0-12 --inventory-only --no-publish
```

The first command has **no network calls or filesystem writes**. The second checks live GRIB index availability for a recent retained run; replace `YYYYMMDD` with an actual currently available date. Watch for source-specific missing variables or 404s. Verify `python3 -m unittest discover -s tests` and `bash -n run_ai_incremental_cron.sh` before enabling the cron entry.

To process a narrow set of hours locally, without publishing:

```bash
python3 ai_to_zarr.py --date YYYYMMDD --cycle 06 --models all \
  --hours 0-24 --local-workers 8 --remote-workers 3 --max-hour-jobs 8 --no-publish
```

Then remove `--no-publish` (and use `--force-hours` only if actually repairing missing fields in completed lead times). Never delete production Zarr stores to force a retry.

## 4. Separate AI cron

Use the **same executable runtime capture** as the existing GFS/RRFS cron jobs. Run `./capture_converter_runtime.sh` if it has not previously been configured. Do not create a second shared-verification cron: keep the existing verification workflow unchanged. AI ingestion has its own lock and honors the historical-case gate.

```bash
chmod 750 run_ai_incremental_cron.sh
# Add one new line to crontab (replace /path/to/processing):
*/30 * * * * /path/to/processing/run_ai_incremental_cron.sh
```

Runs are logged as `logs/ai_incremental_YYYYMMDD_HHMMSS_PID.log`. No second Python environment or source modules are required. The retry queue is bounded by `AI_MAX_HOUR_JOBS`; completion is incremental, so newly available hours appear without waiting for an entire cycle. Do not assume 00/06/12/18 runs are complete at a fixed minute.

## 5. Existing GFSv16 / analysis panels

The AI viewer reuses your existing `gfs_v16` model catalog. This release adds native **250 hPa wind/height** and **1000 hPa temperature/height** to the existing GFS converter. Newly processed v16 cycles acquire those fields automatically. To repair a recent already-processed v16 cycle:

```bash
python3 gfs_to_zarr.py --date YYYYMMDD --cycle 06 \
  --models gfs_v16 --hours 0-24 --force-hours --workers 3 --skip-marine --skip-profiles
```

Review the result using `--inventory-only` first. These are source-dependent fields: if a GRIB record is absent, the viewer displays an unavailable field. The bottom-right analysis panel is **GFSv16 F000 at the target valid time**; it requires that later GFSv16 analysis cycle to be published, so future forecast valid times may understandably be blank.

## 6. Shared precipitation verification

The shared verification converter now includes `ccpa_1h`, `ccpa_3h`, `ccpa_6h`, native `stageiv_6h`/`stageiv_24h` and optional MRMS `mrms_apcp_6h`/`mrms_apcp_24h`/`mrms_apcp_pass2`. Retention and protected-case lists cover the new product names. Hourly CCPA must be present in the source archive before it can be published; missing hours remain missing rather than fabricated. The viewer chooses a complete native accumulation when available or combines consistent same-provider intervals without overlaps, and only then uses a different complete provider as fallback. It never mixes quality passes in one accumulation.

For a wider historical retry, invoke your established `multi_model_to_zarr_wcoss2.py --verification-only` workflow using a suitable `--lookback-hours` (e.g. 72 or 168), then review the logs and local catalogs. Protect any historical cases before cleanup. Native MRMS add-on products are off by default to limit processing and storage; only enable them when needed.

## 7. Validation and rollback

Check `zarr/catalog_{aigfs,aigefs_mean,hgefs_mean,gefs_mean}.json` for expected variables and usable `available_hours`. The GEFS catalog contains two stores per cycle (0.25 degree surface, 0.5 degree pressure-level); the viewer selects one grid per field and time. The publisher places these catalogs alongside the existing GFS/analysis catalogs under the viewer's `data/` directory. Confirm file permissions. The AI converter never deletes GFS/RRFS directories; its remote cleanup matches only the five AI store prefixes and honors protected cases.

To roll back, remove the AI cron entry (or set `AI_ENABLED=0`), restore backed-up converter source and viewer `config.json`, and leave the generated AI data in place until you have verified they are unneeded. Do **not** delete protected cycles, the shared analysis catalog, existing model data, or the runtime files.

**Testing limitation:** Automated source and synthetic-Zarr regression tests pass. The complete production GRIB/index transfer, actual `wgrib2` crop and live model rendering require validation on your processing and web servers. A pre-existing real-GFS-index regression test is skipped because its fixture files were not supplied.

## GEFS native 0.25-degree companion (v1.4.38)

The official GEFS ensemble-mean `atmos/pgrb2sp25/geavg.tCCz.pgrb2s.0p25.fFFF` inventory includes PRMSL, PRES, TMP/DPT at 2 m, UGRD/VGRD at 10 m and APCP. These are cropped and published on the original 0.25-degree grid. The existing `pgrb2ap5/geavg.tCCz.pgrb2a.0p50.fFFF` stream remains native 0.5 degree and supplies the requested pressure-level fields. Specific humidity at 2 m is not in the checked 0.25-degree inventory and is not fabricated.

The 0.25-degree GEFS companion requires **no special additional GEFS configuration**. Both `AI_MODELS=all` and a legacy explicit `AI_MODELS=gefs_mean` automatically include the new `gefs_mean_p25` companion. Data are published as `gefs_mean_p25_YYYYMMDD_CC00.zarr` and `gefs_mean_YYYYMMDD_CC00.zarr`, both indexed in the **existing** `catalog_gefs_mean.json`. No separate viewer source is necessary. The high-resolution surface grid supports F000–F240, and the older 0.5-degree stream may be requested through F384.

Dry-run:

```bash
python3 ai_to_zarr.py --date YYYYMMDD --cycle 18 --models gefs_mean --plan
```

Inspect real inventories and then process a small test (replace date/cycle with an available run):

```bash
python3 ai_to_zarr.py --date YYYYMMDD --cycle 18 --models gefs_mean --hours 0-12 --inventory-only --no-publish
python3 ai_to_zarr.py --date YYYYMMDD --cycle 18 --models gefs_mean --hours 0-12 --workers 2 --max-hour-jobs 3
```

This keeps any previously published 0.5-degree GEFS store intact and adds the new high-resolution companion. The viewer automatically prefers 0.25-degree surface fields when their requested forecast hours are present, falling back to the 0.5-degree record if the high-resolution product is not yet available. It never combines different grids within a precipitation accumulation. Case protection automatically includes both GEFS stores for previously declared GEFS cases.

`AI_MAX_HOUR_JOBS` applies per internal native-resolution stream, so GEFS now has two streams; initially use small limits if processing or storage is constrained. Existing `--force-hours` semantics do not overwrite completed forecast chunks. Do not delete previously published 0.5-degree Zarr stores.

## Local operational AI sources (converter v1.4.39)

AIGFS, AIGEFS Mean and HGEFS Mean are read from their existing operational GRIB2
directories by default. These sources no longer use NOMADS unless
`AI_SOURCE_MODE=remote` or the optional `AI_NOMADS_FALLBACK=1` is set.
GEFS Mean continues to use AWS.

Add to the existing `converter_config.sh` (preserve other settings):

```bash
export AI_SOURCE_MODE=local
export AI_LOCAL_WORKERS=8
export AI_REMOTE_WORKERS=3
export AI_NOMADS_FALLBACK=0
export EVU_PERF_ENABLED=0
```

The local defaults are `/lfs/h1/ops/prod/com/aigfs/v1.1`,
`/lfs/h1/ops/prod/com/aigefs/v1.0`, and
`/lfs/h1/ops/prod/com/hgefs/v1.0`.
`ai_to_zarr.py --date YYYYMMDD --cycle CC --models all --hours 0-12 --plan`
shows both sources and worker caps without downloading or publishing.
A local file which is not yet present is retried on later polls.

**Historical cron gaps:** Normal catch-up only covers the configured recent
cycles. For older cycles, use an explicit `--date`/`--cycle` backfill with
`--hours` after coordinating with cron to avoid overlapping store writes.
