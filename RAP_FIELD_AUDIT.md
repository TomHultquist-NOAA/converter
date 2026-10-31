# RAP and REFS Mean upper-air field audit

This release includes `audit_rap_case.py` at the root of the **full converter ZIP**. It is read-only and does not call the network.

## Check the October 28 case and the REFS Mean

```bash
cd /path/to/converter
python3 audit_rap_case.py --root "$PWD/zarr" \
  --start-hour 2026102812 --end-hour 2026102812 \
  --refs-mean-cycle 2026102812 --refs-hour 1
```

For a current RAP valid hour, replace the UTC date/time in `--start-hour` and `--end-hour`. This prints store/catalog presence for native and aligned RAP, available/missing 300-hPa height and wind components, derived 300-hPa wind speed, 500-hPa height/winds/vorticity, CAPE and helicity fields. When a REFS Mean initialization is supplied it prints the native mean model's F-hour chunks as well.

**v1.3.3 audit accuracy (retained in v1.3.4):** A previous audit checked `model == "refs_mean"` in `catalog_refs_mean.json`; however the converter publishes the display label `REFS Mean`. A prior `catalog=False` result for a present mean store was therefore not reliable. The corrected audit matches the exact store path and UTC initialization. It also reads staged GRIB manifests under `work/verification_staging` and lists actual field chunks and catalog-covered fields separately for both native and aligned RAP. `generated_utc` is now accepted as the aligned/mean catalog timestamp. This utility remains strictly read-only.

**Interpretation:** A missing native store means RAP was not ingested/converted or was removed by retention; a native store with missing fields means selected records were not present, were missed, or conversion was incomplete. An aligned store is separate and needed for common-model-grid difference maps. A complete local catalog still does not prove that the remote web server files are web-readable. Run the viewer's `rap_diagnostics.html` as a separate browser-side check. The audit cannot distinguish "not in the source GRIB" from "not extracted" until the upstream GRIB inventory is checked.

The published NCEP RAP pressure-file inventory includes native 300-hPa HGT/UGRD/VGRD, 500-hPa HGT/UGRD/VGRD/ABSV, and CAPE at layer definitions including 90–0, 180–0, and 255–0 mb AGL. The `mucape` selection is now restricted to the specified 255–0 mb CAPE layer; 180–0 is **not** silently renamed. A native `CAPE:surface` message is not assumed to exist. Field availability may change by source/valid hour.

## When a RAP hour is actually missing fields

Once the diagnostic confirms missing native chunks, first inspect the archived GRIB inventory or staging manifest and verify that the corresponding pressure-file records exist. To redownload **RAP only** for a specific valid hour, run outside the regular scheduled jobs using `--verification-only --source rap --start-hour ... --end-hour ...` (without `--force` for inventory mismatches), supplying the operational output/work roots and publishing options as used in the cron script. This refreshes native RAP stores and catalog. Then rerun `aligned_analysis.py` with `--products rap_pressure,rap_surface --since YYYYMMDDHH --until YYYYMMDDHH` to repair any aligned fields and publish the corresponding catalog.

The verification workflow checks field-manifest consistency and revisits outdated conversions within its configured lookback; fields explicitly absent from the upstream inventory are not fabricated. For older cases outside default seven-day retention, the corresponding UTC window must be protected before attempting recovery.

## REFS Mean caveats

REFS Mean is retrieved from its **operational native mean product**, not reconstructed from only the five plotted REFS members. Its available vertical inventory may be much sparser than RRFS and HRRR. Missing `h300`/`u300`/`v300` at a requested hour must not be filled by relabeling 250- or 500-hPa data or by averaging five members. Existing `--repair-upper-air` or incremental checking can add fields only when their native mean GRIB records exist. A local missing-chunk audit by itself cannot establish the remote source inventory.

## v1.3.4 current-config coverage and automatic recovery

The updated audit compares existing staging manifests with the current
`verification_sources.json` field keys and prints `STALE MANIFEST` with the
selectors not yet examined. The normal verification workflow now re-ingests and
reconverts these obsolete hours automatically when included in the current
lookback or an explicitly requested UTC window. No global `--force` is needed
for this particular class of older-store inventory mismatch. The original
`selected` and `missing` entries remain available for independent review.

For a historical case outside the configured verification lookback, request an explicit UTC valid-hour window using `multi_model_to_zarr_wcoss2.py --verification-only --source rap --start-hour YYYYMMDDHH --end-hour YYYYMMDDHH`, then run `aligned_analysis.py` for the same window. Schedule these operations while the regular processing jobs are idle, preserving `protected_cycles.json` and local runtime settings.

## v1.3.5: staged field present but absent from conversion

An operational audit can show `mucape` in the staging manifest even when the
committed `.conversion.json` does not include it. This means the stage and
conversion are inconsistent, even if a `mucape/0.0.0` chunk is left in the
native store. The v1.3.5 read-only audit calls this `STALE CONVERSION`. The
converter now cross-checks selected native records against the committed
conversion metadata. Revisit the exact UTC hour with the usual
`--verification-only` backfill (without `--force`), then regenerate aligned
analysis for the hour. Do not manually edit Zarr chunks or catalogs.
