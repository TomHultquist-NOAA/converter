# Sounding profile acquisition — OMD EVU Converter 1.3.8

This component automatically collects pressure-level fields for future finer-vertical-resolution Skew-T soundings (native levels when available). Viewer 1.1.11 and later can load these profile stores through `catalog_profiles.json`; older cases without profiles continue to use the sparse fallback. Existing model maps, observation collection, and verification are unchanged.

## Variables and native levels

At native available isobaric levels between **1000 and 100 hPa**, in 25-hPa coordinate steps, the collector extracts:

- Temperature (`TMP`, kelvin)
- Specific humidity (`SPFH`, kg/kg)
- U and V wind (`UGRD`, `VGRD`, m/s)
- Dewpoint (kelvin), derived from native specific humidity and pressure where available; otherwise uses native DPT or temperature plus native relative humidity (RH) as available. REFS Mean uses its native DPT.

**REFS members m001–m005 and the operational REFS Mean are included.** Member pressure-level output (`prslevnomads`) is coarser than RRFS/HRRR. REFS Mean is read directly from the `ensprod/refs.tCCz.mean.fFF.conus.grib2` product, **not calculated from the five displayed ensemble members**. That operational mean includes the time-lagged RRFS/HRRR contributions. Its published inventory includes native mean temperature at 925, 850, 700, 500, and 250 hPa; native mean dewpoint at 925, 850, 700, and 500 hPa; and native mean U/V winds at 925, 850, 700, 550, 500, and 250 hPa. **Specific humidity is not published in this REFS Mean product**, so the source's DPT records are used directly rather than calculating dewpoint from q. The 250-hPa dewpoint and additional missing vertical levels remain **NaN**, not zero, and are not synthesized through interpolation. Available levels may change between source revisions; `--inventory-only` checks a selected cycle before acquisition.

The default sampled horizontal grid uses every 4th point from 3-km model data (~12-km spacing) and every native grid point for 13-km RAP (~13-km spacing). This produces roughly 16 times as many horizontal profile points as the older 48/52-km sampling. Seven-day retention partly offsets the additional volume but does not eliminate the extra decoding, network and disk demands. **This is point-sounding data, not a map-quality 3-km field.** Low levels below terrain can be missing or unphysical; plot users should be cautious about below-ground pressure levels until terrain masking is available. Units in Zarr follow the metadata rather than assuming all values are °C or kt. Dewpoint is derived from q and p using a water-phase Magnus inversion, or from native T/RH when necessary; missing moisture data remain missing, and these are not complete ice-phase thermodynamic diagnostics.

## Automatic operation

When installed into the existing processing server converter location, `run_model_incremental_cron.sh` invokes the profile collector under its existing model lock after the model poll. The collector defaults to **enabled**, including direct REFS Mean collection, and creates independent `sounding_*.zarr` stores and `catalog_profiles.json`. It processes up to **one new forecast hour per selected model per poll** by default. RRFS and HRRR are polled each run; one of the five REFS members rotates each 15-minute poll to bound memory and network use. Recent RAP analysis profiles are collected separately every poll, with a 2–3-hour availability buffer. Normal model processing continues if any profile product fails; the wrapper records the failure in its log.

Settings can be overridden in `converter_config.sh`:

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

## Integrated case processing (recommended)

`create_case.py --execute` now handles both forecast and RAP sounding-profile creation, full available forecast-hour coverage, remote publication, and the profile catalog automatically. A manual `sounding_profiles.py` command is **not necessary** for case studies. For old completed profiles with incomplete moisture fields, append `--reprocess-soundings` to the same case command; `--mode all` also reprocesses but overwrites map fields. Set `--publish-workers 3` to reuse parallel SSH/SCP transfers, or `--skip-soundings` to intentionally omit profiles. Examples and permission requirements are in [CASE_STUDIES.md](CASE_STUDIES.md).

For standalone diagnostic/recovery use, `sounding_profiles.py` still supports its direct CLI. A new RAP-only `--start-hour YYYYMMDDHH --end-hour YYYYMMDDHH` option processes up to 168 consecutive hourly RAP F000 profiles in one invocation; use `--date`/`--cycle` for the first hour, and `--models rap_analysis`.

## Backfill and manual recovery

**Repairing already-published pressure profiles (including overnight hours):**
After updating the converter, `--reprocess-hours` rebuilds a selected set of
previously processed hours instead of skipping their completion markers. For
example, while the converter cron is paused and the target cycle is preserved:

```bash
python3 sounding_profiles.py --date 20261030 --cycle 00 \
  --models rrfs,hrrr --hours 0-6 --max-hours-per-model 7 \
  --reprocess-hours --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --publish-workers 3
```

For a complete protected case, prefer the integrated `create_case.py` workflow rather than running separate forecast and RAP commands.
An hour can still have sparse native vertical coverage when the source product
lacks levels or humidity. Reprocessing cannot create genuinely absent native
fields. Confirm `catalog_profiles.json` and profile chunks are visible via the
web server before interpreting a six-level fallback as the actual sounding.

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

Viewer 1.1.11 and later consumes `data/catalog_profiles.json` and the per-hour Zarr arrays (`t`, `td`, `q`, `u`, `v` (REFS Mean advertises `t`, `td`, `u`, and `v`; `q` is deliberately missing)), handle missing levels, mask points below ground, and present winds in knots if labeling them as kt. Viewer 1.1.11 and later reads those arrays; missing profiles continue to use sparse sounding fields.

## Parallel sounding-hour decoding (v1.4.0)

The standalone sounding utility now accepts `--hour-workers N` (1–4, default 2)
for concurrent conversion of independent forecast hours. Hour workers use
unique working directories and Zarr array chunk names and write their own
completion markers only after all five arrays are committed. Store metadata
is initialized before parallel conversion; the shared profile catalog and
publication remain ordered after all workers complete. The case utility
forwards `--sounding-workers N` to this setting automatically, and the normal
cron wrapper uses `SOUNDING_PROFILE_HOUR_WORKERS` (default 2).

Use 1 worker if simultaneous `wgrib2` decompression causes excessive memory,
disk or network contention. The default 2 is a conservative starting point,
not a measured performance guarantee. The log now includes start and elapsed
seconds for each sounding hour, making slow source downloads and decode
bottlenecks more visible. An interrupted case should normally be resumed with
`--mode missing`, without `--reprocess-soundings`, to skip completed hours.

## Native surface-level data (v1.4.5)

The collector now requests surface pressure (`PRES:surface`, Pa) and 2-m temperature/dewpoint (`TMP`/`DPT:2 m above ground`, K) on the original model grid, then spatially samples them using the sounding store's established grid stride. The extra Zarr arrays `sp`, `t2m`, and `td2m` have `(valid_time,y,x)` shapes. The companion viewer places the valid 2-m temperature and dewpoint at the native surface pressure and hides below-ground isobaric levels.

`--mode missing` in `create_case.py` also refreshes pre-v1.4.5 sounding hours with a bounded surface-only repair, preserving the existing temperature, humidity, and wind-pressure chunks. If source products do not supply an exact surface field, the value stays missing; pressure-level-only profiles remain visible without claimed terrain screening.


### Surface wind vectors (profile revision 4)

`u10` and `v10` are native 10-m AGL wind components (m/s), sampled from exact UGRD/VGRD messages at `10 m above ground` in the source model's native surface file. They are displayed at the native surface-pressure anchor with the existing 2-m T/Td for visual continuity; they are not 2-m winds or interpolated vertical-profile winds. When both components exist, the native wind level count includes this near-surface level. A normal sounding rerun recognizes revision 3 hours and upgrades only the surface arrays (without regenerating isobaric chunks). If either vector component is unavailable, no surface barb is drawn and the count does not increase.
