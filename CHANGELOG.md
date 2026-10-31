# v1.3.6 — Bounded performance tuning (2026-10-30)

- Increased normal converter worker defaults: main cron 16, incremental watcher 12, case creation 12 (1–20 supported). Existing private overrides are preserved.
- Added optional parallel aligned-analysis regridding using worker-local projection-map caches (`--workers 1–4`, normal cron/cases default 2, direct script default 1). Catalog construction and publishing remain serialized.
- Kept OpenMP/BLAS one-thread-per-process, existing locking, protected-case retention, and model/analysis field inventories unchanged.
- Added parallel scheduling regressions and documented resource tuning and limitations in RELEASE_1_3_6.md.

## 1.3.5 (2026-10-30)
- Verification workflow no longer skips internally complete RAP/analysis stores when staged supported fields are absent from committed conversion metadata. This catches RAP MUCAPE staged-but-not-converted cases.
- Rebuilds affected hours when they reenter verification processing, without requiring `--force`; honors explicitly absent GRIB fields and derived output fields.
- Updated read-only RAP audit flags staged-but-unconverted fields, with regression coverage.

## 1.3.4 (2026-10-30)
- Recognize obsolete staged GRIB field manifests when deciding whether a completed verification analysis store can be skipped; automatically re-inventory when current requested fields were never examined.
- Detect changes in native GRIB level/layer selection before reusing an already staged source (including prior RAP MUCAPE selector corrections).
- Distinguish fields truly absent from native GRIB inventory, recorded as missing, from fields omitted by an older configuration. Refresh the RAP audit output accordingly.
- Align verification source retention configuration with the established seven-day default. Add targeted October 28, 2026, 12Z RAP recovery instructions.

## 1.3.3 (2026-10-30)
- Fixed REFS Mean catalog verification in the read-only RAP case audit: catalog model uses the display label, so match by store path and UTC initialization.
- Expanded audit to display actual native/aligned chunk fields, corresponding catalog field coverage, staging manifest selected/missing records, and proper catalog timestamps.
- Added regression tests; no forecast conversion, cron, publishing, or retention changes.

## 1.3.2 (2026-10-30)
- included the read-only RAP/REFS Mean field audit in the complete converter package;
- expanded diagnostic coverage for native and aligned RAP 300-hPa, 500-hPa, CAPE and storm-relative helicity fields;
- corrected the RAP MUCAPE selection to the native 255–0 mb AGL CAPE layer, without substituting unsupported CAPE definitions;
- documented targeted valid-hour RAP repair while retaining seven-day default and protected-case safeguards.

## 1.3.2 — October 30, 2026
- Include the read-only `audit_rap_case.py` utility at the package root to check local RAP native and model-aligned Zarr stores and catalogs by UTC valid hour.
- Integrate the audit instructions into the README so a separate script download is unnecessary.
- No changes to numerical processing, cron behavior, sounding resolutions, publication or retention compared with converter 1.3.1.

## 1.3.1 — October 30, 2026
- Increase sounding-profile sampling to ~12 km (RRFS/HRRR/REFS models/REFS Mean) and native ~13 km (RAP); no change to native vertical level inventories or mean-source selection.
- Restore seven-day default retention for unprotected models, analyses, observations, profiles and logs in both cron wrappers and CLI maintenance commands.
- Add explicit dry-run-first `migrate_profile_grid.py` for existing 48/52-km sounding stores; preserve originals in dated local backups and retain safe incremental publication snapshots.
- Update sounding operations, deployment and capacity documentation; add resolution and migration tests.

## 1.3.0 — Native sounding profiles (October 30, 2026)

- Operational, bounded native pressure-level sounding acquisition for RRFS, HRRR, REFS m001–m005, and RAP analysis. REFS stores retain their available coarser vertical spacing; missing native levels/fields stay missing, not interpolated.
- Pressure-specific conversion of specific humidity into dewpoint; separate, spatially sampled Zarr v2 profile stores and an independently published profile catalog.
- Incremental model cron support with rotating REFS member, hourly RAP analysis, 14-day profile cleanup and protected-case exceptions; core model conversion remains unchanged.
- Index-only availability diagnostics, historical backfill, and release tests. Viewer 1.1.0 does not yet consume these new profile stores.

## 1.2.5 — 2026-10-30

- Add `repair_case_publication.py` to compare locally converted NOHRSC case data to web server stores/catalog and optionally restore only case NOHRSC files and atomically promote a safe merged analysis catalog.
- Extend `audit_case_data.py` with F024/F048/F072 ASNOW coverage for each model, identifying exact missing cumulative inputs for 24-hour snowfall.
- No changes to operational model/verification GRIB conversion, existing crontab jobs, or retention policy.

## 1.2.4 — October 30, 2026
- Continue historical METAR/mPING collection past a failed UTC hour without disguising the failed hour as a successful empty result; preserve nonzero final status.
- Add a read-only per-case NOHRSC/observation audit across staging, Zarr, local catalog, and optionally published files.
- Persist NOHRSC conversion exception details for retry diagnostics and report missing snowfall GRIB field inventories.
- No added dependencies or changes to model field conversion/retention/cron configuration.

## v1.2.3 — 2026-10-30

Adds preliminary SPC storm-report ingestion, atomic publication, historical backfill, case-study integration, and protected 14-day retention. See CHANGELOG.md.

## 1.2.2 — October 30, 2026
- Add 300-hPa U/V winds, geopotential height and derived speed; include RAP F000/analysis fields.
- Fix grossly inflated RRFS/REFS bulk shear with source-aware native-unit derivation; add targeted historical shear repair utility.
- Add source-inventory audit tool for SCP/STP native short names; never fabricate missing indices.
- Apply 14-day retention to dated incremental/verification logs, rotated cron stderr, hourly observation reports, expired model publish state, and stale remote transfer files, respecting protected case observation times.
- Add explicit diagnostics and regression tests while preserving current cron scheduling and Zarr schema.

## 1.2.1 — October 30, 2026
- Correct NOHRSC 6-/24-hour snowfall unit validation to parse the bracketed variable description from `wgrib2 -V`, with a fallback for explicit `units=` metadata.
- Normalize verified snow-depth units (m, mm, cm, inches) to meters; reject ambiguous/non-depth units instead of guessing.
- Retain existing NOHRSC grid184 reprojection, source provenance, native/derived snowfall windows, retry/publishing/retention behavior, and the case-study utility. No change to model processing or cron configuration.
- Add unit-parser regression tests covering expected provider metadata formatting and unsafe inputs.

## 1.2.0 — October 29, 2026
- Add `create_case.py` to acquire and publish one or more selected six-hour model cycles and their full verifying-analysis and Synoptic/mPING valid-time coverage.
- Add exact historical UTC verification processing (`--start-hour` / `--end-hour`), divided into 168-hour windows; respect protected old analyses beyond normal retention.
- Add `--force-missing-check` for manual incremental case backfills without waiting for the 15-minute watcher's missing-field cooldown.
- Merge selected model cycles into the operational `protected_cycles.json` atomically, retain all prior pins and protected analysis rules, and save timestamped backups. Acquire both existing cron locks.
- Preserve standard Zarr, verification/analysis, aligned-analysis, observation GeoJSON, catalog publishing and cron scripts; no new packages or cron entries required.
- Add `CASE_STUDIES.md` and regression tests for time windows, protection, dry runs, publication commands and failure-safe operation.

## 1.1.2 — 2026-10-29
- Correct incremental logging for completed forecast hours with no newly available optional GRIB records; retain their data and retry cadence.
- Distinguish optional-field checks from genuinely unavailable new hours in logging and run summaries.
- Add regression tests for real inventory/no-match checks and preservation of completed-hour chunks.

# OMD EVU Converter — Changelog

## 1.1.1 — October 29, 2026
- Fix a `TypeError` in incremental missing-field recovery when a completed hour is repaired without a named `repair_kind`. Use the explicit fallback `fields` for null or empty repair kinds and preserve existing field coverage, selections, products, and staged accumulation metadata.
- Add regression tests for unnamed incremental repairs, explicit snowfall repairs, and missing/empty repair kinds. No GRIB decoding, Zarr format, publishing, scheduling, or model field mappings changed.
- The interrupted polling run may have written valid local chunks without reaching catalog generation or publishing; subsequent polls can safely resume and publish the accumulated changes.

## 1.1.0 — October 29, 2026
- Introduce 15-minute incremental forecast ingestion with bounded discovery, per-field missing-chunk detection, retry cooldowns and fair per-model scheduling.
- Add publication snapshots, full-then-delta transfers, atomic remote chunk replacement, catalog-last publishing, and failed-transfer recovery.
- Derive missing REFS Mean 500-hPa absolute vorticity from native ensemble-mean 500-hPa winds using Lambert/spherical geometry, with explicitly derived field provenance.
- Split model scheduling from hourly verification and observations, preserving delayed REFS Mean repair, daily cleanup, 14-day retention and protected-cycle exceptions.
- Add targeted tests and a separate operational activation template; no new Python dependencies, no viewer schema change.

## 1.0.3 — October 29, 2026
- Add a separate stdlib-only Synoptic METAR and mPING hourly precipitation-type observation collector with ±30-minute valid-hour matching and classification to the viewer's existing categorical colors.
- Limit Synoptic queries to official ASOS/AWOS network 1 and disable synthetic high-frequency METARs. Use mPING's authenticated report endpoint, category/time/geographic filters, and bounded pagination.
- Publish sanitized GeoJSON through the established web server SSH/SCP mechanism only when rights are explicitly confirmed. Keep provider credentials in an excluded mode-0600 local file; never embed credentials in web output.
- Integrate observation collection into the existing hourly cron process as an optional, disabled-by-default activity. Enable and configure separately from release files; maintain existing protected cycles, forecast conversion, verification, and retention behavior.

## 1.0.2 — October 29, 2026
- Add model-native MSLP extraction (`PRMSL`, `MSLET`, `MSLMA`) and targeted `--repair-mslp` backfill.
- Extend RAP F000 analyses with sea-level MSLMA, pressure-level height/wind and 500-hPa vorticity, 0–6 km bulk shear, SRH, and supported CAPE diagnostics.
- Keep unsupported RAP 0–1 km bulk shear, 1-hour/run-max UH, SCP and STP unpopulated.
- Preserve the existing 14-day retention, catch-up, cron, NOHRSC, MRMS, and protected-cycle workflows.

## 1.0.1 — October 29, 2026
- Recognize explicit day-unit (`0–1 day acc`, `0–2 day acc`, etc.) snowfall intervals as 24/48/etc. forecast hours, fixing omitted F024/F048/F072 ASNOW totals in supported models.
- Add `--repair-snowfall` for safe recovery of previously missing ASNOW chunks from original staged GRIB data, followed by ASNOW-only source retrieval for any remaining holes.
- Preserve unrelated existing Zarr chunks, accurate accumulation endpoints, retention, cron settings, remote publication, and viewer compatibility. No viewer update required.

## 1.0.0 — October 29, 2026 (first public release)
- Establish first public version number and release documentation; previous internal builds are now identified as 0.x.x.
- Standardize the deployment README as an audience-neutral operational guide.
- No changes to GRIB mappings, model selection, recovery, Zarr schema, cron scheduling, analysis processing, publishing, or retention from internal 0.9.3.

## Earlier internal development builds (0.x.x)

## v0.9.3 — 2026-10-29
- Stop downloading NOHRSC 48-/72-hour files: native 6h at 00/06/12/18 and native 24h at 00/12 only.
- Map 48h analyzed snowfall to two 24h inputs in the viewer, and add virtual MRMS 24-/48-hour precipitation sources. Keep legacy archival/retention support.
- Preserve existing cron, latency, recovery, retention, and publishing.

## v0.9.2 — 2026-10-29
- Strict hourly MXUPHL and run-maximum UH. Removed instantaneous UPHL. SCP/STP native UPP GRIB aliases and fixed-layer STP support.


## 0.9.1 — October 29, 2026
- Document 1-, 3-, and 6-hour virtual precipitation and snowfall intervals derived in viewer v0.50.1 from existing cumulative model Zarr arrays.
- Add viewer-facing analysis source mappings for MRMS hourly QPE (1/3/6-hour precipitation) and explicitly mark 1-/3-hour snowfall analysis unavailable. NOHRSC 6-hour snowfall unchanged.
- No changes to GRIB acquisition, processing, hourly verification, aligned analysis, recovery, cron, retention, publishing, or the Zarr schema.

## 0.9.0 — October 29, 2026
- Switch REFS Mean accumulated precipitation extraction from `mean` to `pmmn`, matching the existing PMMN composite reflectivity source; label them **PMM accumulated precipitation** and **PMM composite reflectivity**.
- Add NOHRSC gridded snowfall analyses at 6, 24, 48, and 72 hours, with cycle-dependent eligibility, two-hour publication delay, **72-hour NOHRSC recovery** (the existing 20-hour other-analysis recovery is preserved), grid184-to-3 km Lambert regridding, snowfall depth-unit validation, retention, publishing, and optional case-study protection.
- Add exact 925/850/700/500-hPa geopotential heights and U/V winds plus 500-hPa absolute vorticity; optional meteorological diagnostic fields, including 0–1 and 0–6 km bulk shear calculated from the VUCSH/VVCSH vertical gradients times layer depth; optional SRH/CAPE/UPHL/MXUPHL/SCP/STP require an actual matching record.
- Add explicit repairs `--repair-upper-air` (existing processed model hours) and `--repair-refs-pmm-apcp` (older REFS Mean data, preserving `.legacy_mean_apcp` backups). Existing processed model hours otherwise remain untouched.
- Preserve 3-cycle model catch-up, cron locking, 14-day retention, protected cycles, verification and publication.

## 0.8.5 — October 29, 2026
- Correct new HRRR `ptype` Zarr metadata label to **Derived precipitation type** (RRFS and REFS labels unchanged).
- Preserve all category codes, conversion calculations, auto-backfill, verification, publishing, and retention logic.
- Viewer v0.49.4 provides the visible HRRR panel/legend and sampled categorical names without requiring converter redeployment.

## 0.8.4 — October 29, 2026
- Default CATCHUP_CYCLES changed from 1 to 3. Revisit newest eligible cycle plus the previous three six-hour initialization cycles; skip already-processed forecast hours.
- Preserve hourly 20-hour verification retries (includes all missing eligible hours in the most recent six hours and delayed URMA).
- Clarify RRFS/REFS-member precipitation-type and REFS Mean PMMN reflectivity variable descriptions for newly initialized Zarr stores.
- Keep the supplied 0.8.3 converter, publishing, aligned verification, retention, and protection behavior.

## 0.8.3 — October 28, 2026 (supplied release)
- Previous full-package converter with the REFS Mean `mean`/`pmmn` mapping fix and aligned-analysis support. Refer to README.md and CHANGELOG.md for historical detail.

- REFS Mean sounding profiles now use native operational ensemble-mean temperature, dewpoint, and winds, preserving time-lagged contributors rather than computing a mean from five displayed members.
