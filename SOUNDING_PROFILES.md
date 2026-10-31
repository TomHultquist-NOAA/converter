# Sounding profile acquisition — OMD EVU Converter 1.3.1

This component automatically collects pressure-level fields for future finer-vertical-resolution Skew-T soundings (native levels when available). Viewer 1.1.1 and later can load these profile stores through `catalog_profiles.json`; older cases without profiles continue to use the sparse fallback. Existing model maps, observation collection, and verification are unchanged.

## Variables and native levels

At native available isobaric levels between **1000 and 100 hPa**, in 25-hPa coordinate steps, the collector extracts:

- Temperature (`TMP`, kelvin)
- Specific humidity (`SPFH`, kg/kg)
- U and V wind (`UGRD`, `VGRD`, m/s)
- Dewpoint (kelvin), derived from specific humidity and pressure except REFS Mean native DPT

**REFS members m001–m005 and the operational REFS Mean are included.** Member pressure-level output (`prslevnomads`) is coarser than RRFS/HRRR. REFS Mean is read directly from the `ensprod/refs.tCCz.mean.fFF.conus.grib2` product, **not calculated from the five displayed ensemble members**. That operational mean includes the time-lagged RRFS/HRRR contributions. Its published inventory includes native mean temperature at 925, 850, 700, 500, and 250 hPa; native mean dewpoint at 925, 850, 700, and 500 hPa; and native mean U/V winds at 925, 850, 700, 550, 500, and 250 hPa. **Specific humidity is not published in this REFS Mean product**, so the source's DPT records are used directly rather than calculating dewpoint from q. The 250-hPa dewpoint and additional missing vertical levels remain **NaN**, not zero, and are not synthesized through interpolation. Available levels may change between source revisions; `--inventory-only` checks a selected cycle before acquisition.

The default sampled horizontal grid uses every 4th point from 3-km model data (~12-km spacing) and every native grid point for 13-km RAP (~13-km spacing). This produces roughly 16 times as many horizontal profile points as the older 48/52-km sampling. Seven-day retention partly offsets the additional volume but does not eliminate the extra decoding, network and disk demands. **This is point-sounding data, not a map-quality 3-km field.** Low levels below terrain can be missing or unphysical; plot users should be cautious about below-ground pressure levels until terrain masking is available. Units in Zarr follow the metadata rather than assuming all values are °C or kt. Dewpoint is derived from q and p using a water-phase Magnus inversion, and should not be interpreted as a complete ice-phase thermodynamic diagnostic.

## Automatic operation

When installed into the existing processing server converter location, `run_model_incremental_cron.sh` invokes the profile collector under its existing model lock after the model poll. The collector defaults to **enabled**, including direct REFS Mean collection, and creates independent `sounding_*.zarr` stores and `catalog_profiles.json`. It processes up to **one new forecast hour per selected model per poll** by default. RRFS and HRRR are polled each run; one of the five REFS members rotates each 15-minute poll to bound memory and network use. Recent RAP analysis profiles are collected separately every poll, with a 2–3-hour availability buffer. Normal model processing continues if any profile product fails; the wrapper records the failure in its log.

Settings can be overridden in `incremental_model_config.sh`:

```bash
SOUNDING_PROFILES_ENABLED=1
SOUNDING_PROFILE_MODELS=rrfs,hrrr
SOUNDING_PROFILE_REFS_ENABLED=1
SOUNDING_PROFILE_REFS_MEAN_ENABLED=1
SOUNDING_PROFILE_RAP_ENABLED=1
SOUNDING_PROFILE_MAX_HOURS=1
SOUNDING_PROFILE_GRID_STRIDE=4
SOUNDING_PROFILE_RAP_STRIDE=1
```

For consistency with the seven-day defaults, set the shared retention value in the existing optional local configuration if it currently overrides 7:

```bash
RETENTION_DAYS=7
SOUNDING_PROFILE_GRID_STRIDE=4
SOUNDING_PROFILE_RAP_STRIDE=1
```

**Existing profile stores from v1.3.0 cannot be reused at a different grid stride**; the collector refuses to mix the old and new sampling within a store. When both cron jobs are idle, preview the migration from the converter directory:

```bash
python3 migrate_profile_grid.py --output-root "$PWD/zarr" --work-root "$PWD/work"
```

If the report lists incompatible sounding stores, run:

```bash
python3 migrate_profile_grid.py --output-root "$PWD/zarr" --work-root "$PWD/work" --apply
```

This **atomically moves** only incompatible local sounding stores into dated `work/profile_grid_backups/` directories. It does not delete old data, model maps, verifying analyses, observations, remote stores, or protected-cycle settings. It preserves publishing snapshots so the changed grid metadata/chunks can be synchronized to web server on subsequent collection and publication. **The replacement profile coverage grows gradually**; any protected historical cycles needing upgraded profiles require a separate manual profile backfill. Do not remove the backups until the new sounding profiles and web server catalog have been verified. Backup copies continue consuming disk space until explicitly cleared. In existing deployment environments, ensure both cron processes are stopped before migration; the utility also refuses to proceed while the model lock is held.

Set `SOUNDING_PROFILES_ENABLED=0` to stop collection without affecting existing maps. Logging goes to existing `logs/model_incremental_*.log`. This package does not alter crontab schedules. The model cron must already be active for automatic collection; profiles will not populate retroactively unless backfilled.

## Checking what a source really provides

The **index-only** command prints complete and incomplete pressure-level coverage, including native REFS Mean DPT, without converting or publishing any profile fields. Run it with the captured processing server Python environment:

```bash
cd /path/to/converter
python3 sounding_profiles.py --date 20261028 --cycle 12 \
  --models rrfs,hrrr,refs_mean,refs_m001,refs_m002,refs_m003,refs_m004,refs_m005 \
  --hours 12 --inventory-only
```

This requires access to the configured data source but not `wgrib2`; a missing upstream index is reported as missing rather than fabricated. Use `--models rap_analysis --date YYYYMMDD --cycle HH` separately for an hourly RAP analysis.

## Backfill and manual recovery

Example (initial local-only test):

```bash
python3 sounding_profiles.py --date 20261028 --cycle 12 \
  --models rrfs,refs_m001 --hours 0-6 \
  --max-hours-per-model 2 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" --no-publish --verbose
```

Without `--hours`, the collector picks hours already processed by the map converter. Omitting `--no-publish` publishes profiles to web server after successful conversion, then publishes the separate profile catalog **last**. Existing 3-km model stores are not touched. For locally prepared profile stores:

```bash
python3 sounding_profiles.py --date 20261028 --cycle 12 \
  --models rrfs,refs_m001 --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --publish-only
```

Model forecasts and RAP analysis use different grid definitions. RAP's native grid metadata are read from the GRIB itself before building its coarsened store. There is no need to install external Python libraries.

## Retention, publication, and security

Profile data are retained for 7 days by default. The daily incremental cron retention phase removes **only expired, unprotected profile stores**; protected model cycles also protect their profile stores, and saved forecast-cycle analysis windows protect RAP profiles. Expired profile directories are removed from web server after the remote profile catalog is updated. Profile cleanup is separate from, and does not remove, model maps, analyses, or observations.

The live `protected_cycles.json`, credentials, `converter_cron_env.sh`, runtime paths, and locally downloaded case data must not be committed to a public repository. Do not run manual profile backfills concurrently with the incremental model cron; use the existing operational lock discipline. The grid-stride setting must remain consistent for an existing store; use `migrate_profile_grid.py` to prepare a controlled switch. A change in spatial sampling does not add pressure levels or modify the native vertical inventory.

Viewer 1.1.1 and later consumes `data/catalog_profiles.json` and the per-hour Zarr arrays (`t`, `td`, `q`, `u`, `v` (REFS Mean advertises `t`, `td`, `u`, and `v`; `q` is deliberately missing)), handle missing levels, mask points below ground, and present winds in knots if labeling them as kt. Viewer 1.1.1 and later reads those arrays; missing profiles continue to use sparse sounding fields.
