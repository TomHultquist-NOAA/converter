## v1.4.50 — 2026-11-10
- Default Weather Events headline limit 15; soft ranking de-duplication of nearby same-category events; full candidates remain unchanged.

## v1.4.49 — 2026-11-10

- Expose `recent-cycles` through independent Weather Events cron configuration; remove hard-coded publisher value.
- Expand the independent scheduled scanner sampling default coherently from F012..F048 (12-hour step, 4 frames) to F006..F072 (6-hour step, 12 frames); selected-highlight cap 20 -> 25.
- Validate scheduled sampling counts as positive integers and document existing-installation config migration, resource tradeoffs and fallbacks.
- Preserve all operational converter schedules and resources, scanner meteorological thresholds, historical alert enrichment, publisher safety gates, and viewer outputs.

## v1.4.48 — 2026-11-10

- Experimental Weather Events scene presentation now reserves forecast categories and contributing models before filling intensity-limited slots.
- Added optional historic IEM VTEC + storm-based watch/warning/advisory correlation, distinct provenance, date/location sampling, and explicit outage/forecast-future statuses. Alert issuance is not verified impact.
- Added low-priority standalone locked scheduled scanner and guarded atomic JSON publisher for local directory or SSH destination; requires explicit installation and configuration, defaults to preview only. No operational cron or Zarr modifications.

## v1.4.47 — 2026-11-10

- Limit tropical high-CAPE-only **selected highlights** to one by default (configurable `--max-tropical-cape-highlights 0..N`); keep all detections and all candidate-group summaries without changing geographic masks or thresholds.
- Label CAPE-only situations as high convective instability rather than implying observed convection; identify quota exclusions in candidate summaries.
- Fix stale scene-omission notes when the full distinct-scene list fits the scene cap. Distinguish forecast scenes from different initializations even when their valid times match.
- Preserve Python 3.6-compatible standard library, read-only scanning, model data, conversion, cron, publication and retention behavior.

## v1.4.46 — 2026-11-10

- Reject below-ground 850-hPa GFS temperature differences using matching native surface-pressure fields from both model versions; fail closed if a supporting field/hour is absent and report mask coverage diagnostics.
- Add compact `candidate_group_summaries` for every candidate situation, with selected rank/status, so ranking can be reviewed without uploading full raw detections.
- Record total distinct and omitted scenes when a group exceeds the scene cap, and include category/model counts and review-context notes.
- Rename mixed precipitation/snowfall situations to reflect the dominant precipitation category rather than implying a primarily snow event.
- Preserve existing thresholds, geographic footprints, event ranking, Python 3.6 standard-library dependencies, and operational converter / cron / publication / retention behavior.

## v1.4.45 — 2026-11-10

- Bound experimental event groups by overall spatial diameter and time span, eliminating unrestricted transitive chaining of unrelated storms.
- Select the latest compatible common GFSv16/GFSv17 initialization within a bounded lookback, even when the two models have different newest cycles.
- Record clearer GFS comparison coverage and group geographic span diagnostics.
- Preserve read-only operation, Python 3.6.8 compatibility and all operational converter, cron, publication and viewer behavior.

## v1.4.44 — 2026-11-10

- Refined the experimental event scanner with grouped weather situations, diverse ranking, and detailed detector field-hour coverage diagnostics.
- Tightened broad Caribbean open-water selection while preserving Central America and model-native coverage limits.
- Added additive experimental JSON schema v2 with `event_groups`, model/category breakdowns and per-model missing-data explanations.
- Operational conversion, publication, retention, cron, and viewer functions unchanged.

## v1.4.43 — November 9, 2026

- Add isolated read-only discovery of notable forecast events from completed Zarr v2 stores; Python 3.6.8 and standard library only.
- Apply separate geographic policies: regional CONUS and vicinity versus GFS/AI North America, Hawaii, Puerto Rico and adjacent areas; intersect masks with published native grid coverage at coarse and refined scan stages.
- Add configurable preview schema, threshold/region examples, event rankings and GFS version comparisons; preserve operational converters and crons unchanged.

## v1.4.42 — November 9, 2026

- Add whole-poll process-tree performance sampling to the AI incremental cron, using the existing common settings and bounded retention; matching GFS and regional cron samplers remain unchanged.
- Add fine-grained GFS and AI forecast-hour, local/remote acquisition, GRIB decoding, Zarr commit, sounding, marine, difference materialization, catalog and publication timings. Include model/hour labels, run configuration and selected-work counts without exposing credentials or source URLs in telemetry.
- Record GFS historical-case process-tree resource use when performance monitoring is enabled.
- Correct concurrent metric attribution: thread-local counters prevent overlapping worker threads from double-counting other workers' compression, byte and write-time metrics, while nested timing-only sub-stages avoid counting the same metrics twice.
- Add read-only `summarize_performance_logs.py` to inspect recent completed-report coverage. Monitoring remains opt-in, uses only the standard library and `/proc`, and never changes processing, worker limits, locks, files or publication semantics.

## v1.4.41 (2026-11-09)

Native 925-hPa specific humidity and dewpoint support for the GFS, AI, and regional model pipelines, with same-level thermodynamic derivation where required. Existing fields and source paths retained.

## v1.4.40 — 2026-11-09
GFS native 1000-hPa wind components added; shared model-set architecture preserved. See RELEASE_1_4_40.md.

## 1.4.39 — Local AI GRIB access, GFS field registry, monitoring pause (2026-11-08)

- Prefer local operational GRIB2 files for AIGFS, AIGEFS Mean and HGEFS Mean.
  Retain GEFS mean on AWS with independent local/remote worker caps (8/3 defaults).
  NOMADS fallback for local models is opt-in only.
- Use the GFS-specific field registry for the newly added 250/1000 hPa native
  fields, eliminating `KeyError: h250` without expanding RRFS/REFS ingestion.
- Pause performance sampling by default across all cron wrappers, standalone
  monitored utilities, and historical case workflow. Ordinary logs stay active.
  Include a dry-run-first cleanup utility for superseded performance reports.
- Keep protected-case rules, previously published Zarr, and verification
  ingestion unchanged.

## v1.4.38 — 2026-11-08

Added GEFS 0.25-degree native ensemble-mean surface ingestion and combined-resolution catalog; preserved legacy 0.5-degree data and protected cases.

## v1.4.38 — 2026-11-08

Shared CCPA/Stage IV/MRMS improvements, four AI/GEFS model sources, native-grid Zarr, dedicated incremental cron, GFS 250/1000-hPa fields, protected retention and synthetic regression coverage. See RELEASE_1_4_38.md.

## v1.4.35 — 2026-11-06

- Documentation/GitHub readiness refresh only. Processing/publishing Python source is unchanged from v1.4.34.
- Corrected current-version README references; added architecture/deduplication audit, Linux deployment and contribution checklists.

## v1.4.34 — 2026-11-06

- Removed user-facing artificial maximum worker-count limits across case, GFS, RRFS/REFS, alignment, sounding, GLM, marine and publication workflows.
- Keep positive-integer worker validation, single-threaded native decoders, protection locks and all non-concurrency safety limits.
- Converter output schema, published product names, and existing processing defaults unchanged.

## v1.4.33 — 2026-11-06

Adds GFS-grid-aligned MRMS instantaneous precipitation-rate source to verification conversion and catalog publication; see RELEASE_1_4_33.md.

## v1.4.32 — 2026-11-06

CCPA acquisition and complete-window precipitation analysis priority for RRFS/REFS; see RELEASE_1_4_32.md.

## 1.4.31 (2026-11-05)
- CCPA 6-hour-group archive paths and delayed retry window corrected.
- GFS-aligned Stage IV and MRMS analysis stores, catalog/publication/retention handling added.
- GFS supplemental sounding functionality unchanged.

## 1.4.30 (2026-11-05)
- Validated all 37 GFS sounding levels against real primary/companion IDX files; added RH/dewpoint coverage diagnostics and regression fixtures.
- Preserved existing primary-only map processing and backward-compatible sounding output.

## v1.4.28 — GFS sounding td2m chunk repair

- Map `dpt2m` to the native sounding `td2m` chunk during GFS profile commit. No new data retrieval needed.
- Existing hours require targeted reprocessing; no change to vertical-level selection.

## v1.4.27 — Independent retention policies
- Add independent RRFS/REFS/HRRR, GFS, and observations/analyses retention settings with backward-compatible `RETENTION_DAYS` fallback.
- Keep the protected-case rules and explicit allow-listed local/remote deletions.
- Use the observation cutoff for native/aligned analyses, RAP soundings, GLM, SPC and ptype observations; use the GFS cutoff for GFS profiles.
- Change the daily cleanup success marker when any retention setting changes.
- Add offline local and remote deletion tests for asymmetric retention periods.

## v1.4.26 — HRRR native psfc selection for below-ground terrain masking

See RELEASE_1_4_26.md for details.

## v1.4.25 — retention policy reruns on configuration changes (2026-11-05)

- Store UTC date **and** RETENTION_DAYS in the model incremental retention success marker. Changing the retention window after the daily sweep now triggers another eligible sweep, including remote cleanup and catalog refresh.
- Retain backwards compatibility with existing date-only markers, which trigger one safe reconciliation.
- No changes to protected historical cases, GFS separate cleanup policy, or deletion allowlists.

# Changelog

## 1.4.24 — November 5, 2026 — CCPA and GFS-aligned NOHRSC verification

- Acquire 3/6-hour CCPA and validate accumulation interval.
- Regrid NOHRSC 6/24-hour and CCPA 3/6-hour analyses onto the GFS 0.25° comparison grid; preserve regional verification products.
- Include new stores in converter catalog, publication, retention and protected-case logic.
- Verify operational GRIB2 and remote publishing before deployment.

- 2026-11-03: Corrected GFS cron status detection for completion markers preceded by an SSH login banner on the same line; distinguished success, failure, skip, and incomplete states.
## 1.4.23 — November 2, 2026 — GFS native soundings and kinematic backfill

- Automatic GFS v16/v17 native-quarter-degree sounding stores for F000–F240 on three-hour intervals, preserving exactly available native pressure levels and a separate common sounding catalog.
- Derive absent AGL bulk shear and SRH only where native terrain, pressure height, winds, and storm motion permit; native SRH takes priority.
- Backfill missing GFS profile hours independently of already processed map hours; protect profile data and clean local/remote retention consistently.
- Add explicit GFS profile-only test, skip-profile diagnostic option, and synthetic profile physics/storage regressions.

## v1.4.22 — 2026-11-02 — remove unsupported SCP/STP products

- Remove Supercell Composite Parameter (SCP), effective-layer Significant Tornado Parameter (STP with CIN), and fixed-layer STP from forecast field specifications for all model groups, including GFS v16/v17.
- Retire the SCP/STP-only inventory-audit helper. Preserve SRH, CAPE, bulk shear, and updraft helicity handling unchanged.
- Confirm the v16/v17 F003/F006 inventory feedback (43 versus 48 matched fields); v17 additionally exposes frozr, frzr, tsnowp, sden, and srh01. Complete GRIB-to-Zarr conversion still requires the deployed-environment test.
- Adjust optional-field repair and unsupported-field tests; no changes to grid, publication, protection, or cron scheduling.

## v1.4.19 — 2026-11-02 — comprehensive seven-day retention

- Fix daily maintenance missing the entire day when the exact 03:00 UTC poll was skipped; retry until successful after 03 UTC, with a persisted success marker.
- Make local and web retention include forecast Zarrs, native/aligned analysis including Stage IV, soundings, METAR/mPING, SPC and GLM observations, staging, profiles, publication archives/cache files, and expired generated catalog-upload temporaries.
- Preserve explicitly protected model cycles, their verifying analysis and observation hours, related sounding scratch and historical case logs. Remove or reconcile stale catalog entries and publication-state snapshots.
- Bound ordinary and performance log retention to the configured data window (seven days by default) on the processing server; preserve live lock files, operational configuration, symlinks and unrelated files.
- Add dry-run, protected-case, both-site observation, catalog-safety, and rolling-hour cutoff tests. No changes to forecast variables, Zarr formats or viewer rendering.

## 1.4.18 — delayed Stage IV precipitation analysis

- Collect 1-hour CONUS NCEP Stage IV GRIB2 precipitation analyses, after a conservative 100-minute latency, with an independently configurable 120-hour rolling lookback for later arrivals.
- Reproject Stage IV HRAP/polar-stereographic APCP to the supported 3-km Lambert analysis grid before native Zarr conversion, preserving Stage IV provenance.
- Include Stage IV in the separate aligned-analysis, publication, allowed-path and protected-case-retention workflows. MRMS continues to be published unmodified and remains available as a viewer fallback.
- Preserve distinct 1-hour source identity for trustworthy 3/6/24/48-hour and cycle-to-date comparisons; no cross-source mixing or fabricated incomplete totals.
- Add Stage IV inventory validation, conversion and source-selection regression coverage.

## 1.4.17 — GLM web-readable publication permissions

- Ensure atomic local GLM JSON is written with `0644` rather than the secure temporary-file `0600` default, preventing viewer-side access errors after publication.
- Set remote GLM `lightning/` directory mode to `0755`, and set remote JSON staging files to `0644` before atomic promotion, including cached historical JSON generated by older releases.
- Preserve publication atomicity, cached-hour reuse, field formats, worker counts, and all other converter functionality. Add restrictive-umask and publication permission regression tests.

## 1.4.16 — Nullable manifest revision handling

- Prevent incremental forecast processing from aborting when a per-hour `shear01_revision` or `shear06_revision` manifest value is explicitly `null`. Missing, null, malformed, or non-integral revision values are treated as revision zero (unverified), preserving the existing targeted repair decision instead of raising a `TypeError` during integer comparison.
- Apply the same safe revision normalization to the explicit low-level-shear repair path and to sounding-hour `processing_revision` stamps, covering the other revision comparisons found in the package.
- Preserve the existing chunk-presence gates: shear repair is still limited to hours with the relevant existing components/outputs, and current revision values continue to skip unnecessary repair. No blanket forecast-hour reprocessing, Zarr schema change, worker/concurrency change, or configuration change is introduced.
- Add regression tests for null and malformed revision metadata, both shear revision paths, sounding stamps, and release packaging.

## 1.4.15 — GLM NetCDF thread-safety and bounded download pipeline

- Correct a concurrency defect in GLM collection: worker threads now perform HTTP downloads only; every netCDF4 Dataset open/read/close is performed serially by the collector thread, as required by the non-thread-safe netCDF-C library. This addresses the native `nc4_nc4f_list_add` assertion/core-dump failure observed during the 2026-11-02 hourly GLM cron run.
- Keep `GLM_WORKERS` useful for network throughput while bounding in-flight downloaded payloads to the worker count, avoiding submission of hundreds of completed NetCDF payloads into memory while the serial parser catches up.
- Add per-granule NetCDF parsing time to the existing performance metrics and regression tests proving downloads occur on worker threads while parsing remains on the main collector thread.
- No forecast processing, GLM data schema, Zarr data, case protection, retention, or publication behavior is changed. No new dependencies or configuration changes are required.

## 1.4.14 — Cron invocation visibility and logging

- Create a unique timestamped log immediately when either cron wrapper starts, before runtime/configuration checks, case-priority admission, or writer-lock acquisition. Preflight failures and intentional lock skips are now visible in their own run log instead of only in the fixed crontab stderr capture.
- Record explicit UTC invocation START/END markers, host, PID, outcome (`completed`, `skipped`, `failed`, or `check_only`), exit status, elapsed seconds, and skip reason. Preserve the existing process-tree performance monitor and ensure its child is stopped by the common exit handler.
- Distinguish historical-case admission skips from active-writer lock skips in the final run marker. Extend the existing allow-listed seven-day log cleanup to recognize PID-suffixed per-invocation log names.
- Add `check_cron_status.sh` to display the latest five invocations for each cron workflow and flag runs lacking a completion marker. Keep the existing crontab stdout/stderr redirects as a fallback when the log directory cannot be opened.
- No cron schedule changes, additional cron entries, worker/concurrency changes, or forecast/data-processing changes.


## 1.4.13 — Converter performance instrumentation

- Added optional-by-default-on, standard-library process-tree CPU, memory, process-count, and disk-I/O sampling for cron and historical case runs, including cgroup quota/limit context where Linux exposes it.
- Added per-forecast-hour task timing and observed concurrency, verification ingest/conversion timing, sounding-hour and alignment timing, case-stage queue/run timing, and SSH/SCP transfer duration/byte/concurrency measurements.
- Added compression, GRIB range-download, analysis-download, and GLM download counters without changing the existing processing or worker limits.
- Added JSONL samples/events and JSON/text run summaries with 14-day report retention. Monitoring can be disabled with `EVU_PERF_ENABLED=0`.
- No new Python dependencies; the monitor is observational and does not enforce CPU/memory limits.

## v1.4.12 — 2026-11-01

- Treat historical-case GLM collection as a non-fatal optional observation stage. Incomplete source hours, download errors, and GLM publication failures are reported as warnings and do not fail otherwise successful forecast/analysis case processing.
- Continue to subsequent GLM batches after a failed batch. The collector's atomic hourly output and valid-cache checks ensure incomplete hours are retried on the next execution of the same case, while completed local hours are reused and can be republished.
- Add regression coverage for warning-only GLM batch failures, continuation to later batches, successful primary-case completion, and retry of an hour that previously had incomplete source coverage.
- No forecast conversion, analysis, sounding, Zarr schema, or viewer behavior is changed.

## v1.4.11 — 2026-11-01

- Standardize the trusted shared operational configuration as `converter_config.sh` / `converter_config.example.sh`, reflecting that it controls both cron workflows and historical case processing, not only the incremental model watcher.
- Read the canonical config from both cron wrappers and `create_case.py`; retain a one-release fallback for the old `incremental_model_config.sh` filename and the deprecated `INCREMENTAL_CONFIG_FILE` environment override.
- Keep GLM routine collection, case-backfill opt-in, and `RETENTION_DAYS` in the same shared configuration. No separate GLM retention setting or cron entry is required.
- Correct release ZIP permissions: executable converter scripts are `750`, documentation/config examples/data are `644`, and directories are `755`. Add package-level permission validation.
- Update README, case-study instructions, sounding documentation, release notes, and tests to use the shared converter-wide config name.

## v1.4.10 — 2026-11-01

- Apply the configured `RETENTION_DAYS` policy to local and remote GLM hourly GeoJSON, while preserving valid hours covered by protected historical cases. Remove only stale, allow-listed GLM upload temporaries; preserve active/recent files, symlinks, and unrelated content.
- Use the existing `netCDF4` Python package as the sole GLM NetCDF reader; `h5py` is neither imported nor required. Fall back to a temporary local file if the linked netcdf-c build lacks in-memory read support. Improve GOES granule selection to download only files overlapping the requested hour, including the fractional-tenth boundary granule, rather than an unnecessary full preceding hour.
- Add optional historical-case GLM collection through the shared `converter_config.sh`: `GLM_ENABLED=1` enables routine cron collection, while `GLM_CASE_ENABLED=1` additionally enables bounded case backfill. `create_case.py --skip-glm` provides a per-case override.
- Make `create_case.py` honor shared `MODELS`, `NO_PUBLISH`, `PUBLISH_HOST`, `PUBLISH_DIR`, `PROTECTED_CYCLES_FILE`, and `PTYPE_OBS_SECRETS_FILE`, in addition to existing worker/retention settings, with explicit CLI options taking precedence. Align the case retention fallback with the seven-day operational default.
- Synchronize the shipped shared-config example with the documented seven-day retention and current worker settings; correct stale README/release links, dependency guidance, case/cron configuration guidance, and GLM retention documentation.
- No forecast fields, Zarr schemas, sounding fields, or viewer rendering code are changed.

## 1.4.9 — 2026-11-01

- Profile revision 4 adds native 10-m U/V surface winds for all sounding sources, with surface-only migration of existing hours and explicit metadata for the 10-m measurement height.

- Added standalone hourly GOES GLM L2 lightning flash-centroid collection (`glm_lightning.py`) with source completeness checks, atomic GeoJSON output, optional remote publication, cached hour reuse, and optional default-off unified cron integration. Forecast model processing and previous bulk-shear repairs are unchanged.

## v1.4.8 — RRFS/REFS 0–1 km shear from terrain-relative native winds

- Replaced suspect native `VUCSH`/`VVCSH` 0–1 km components for RRFS and REFS members with a transparent derived wind difference. Original GRIB values at the Minneapolis F010 validation point are approximately the negative of the 10 m wind, rather than the vertical wind difference. This is an upstream field-content anomaly; the precise upstream cause is not established.
- Uses original 3-km grid `PRES:surface` (Pa), 10 m wind components and coincident pressure-level U/V and geopotential heights. Estimates local surface elevation from surface pressure and the lowest two above-ground pressure heights, interpolates the 1-km-AGL wind between ABOVE-GROUND levels only, and stores `U(1 km)-U(10 m)`, `V(1 km)-V(10 m)`, and magnitude in m/s. No extrapolation at 1 km and no below-ground wind levels. Invalid cells remain missing.
- Added the `--repair-lowlevel-shear` converter option and `create_case.py --lowlevel-shear-only` case option for selective correction of completed RRFS/REFS hours. Repairs use existing Zarr pressure/wind data without network access if adequate; otherwise acquire only missing indexed GRIB records. Existing HRRR 0–1 km, all 0–6 km shear, RAP, soundings, analyses, and observations are left unchanged.
- Added per-hour provenance/revision 6 and metadata to distinguish derived from native fields. Preserved v1.4.7 coordination locks and the exclusion of `.lock` files from release ZIPs.
- Added pressure-unit, terrain-screening, offline-repair, component consistency, model-scope and concurrency-safe case-option tests. Synthetic tests are not validation against live operational F010 data.

## v1.4.7 — November 1, 2026

- **Release safety:** Stop packaging live hidden `.lock` files. v1.4.6 inadvertently included them, permitting `unzip -o` to replace a locked inode while cron was still running. Confirmed with a reproducer and prevented by a standardized release archive builder with unit tests.
- Correct historical case/cron coordination when the case utility is run from a separate release/staging directory: lock files now resolve from the operational protected-cycle directory (or an explicit `--lock-dir`), never implicitly from the Python script directory.
- Verify both installed cron launchers declare the same canonical `BASE_DIR`; fail before starting work if the case lock directory differs from that active installed root. Print the resolved lock directory before acquiring locks so operator logs show the exact resource being protected.
- Preserve non-disruptive flock waiting, existing cron admission/legacy-writer locks, and child-process lock inheritance. No changes to conversion, field physics, worker counts, storage format, retention, publication, or viewer.

## v1.4.6 — November 1, 2026

- Prefer native RAP 0–1 km `VUCSH`/`VVCSH` and Bunkers right-mover `USTM`/`VSTM` from the alternate RAP `wrfprs` product when absent from the normal `awp130` analysis stream. Fetch only the required GRIB2 indexed byte ranges and explicitly regrid onto the established RAP analysis grid; never mix projections or silently transfer the entire large alternate product. If the native subset or grid check fails, retain the conservative, clearly marked approximate RAP vertical-profile estimates.
- Validate and, where independently justified, correct RRFS/REFS 0–1 km shear-vector sign against paired low-level winds; preserve scalar magnitude and all 0–6 km shear handling. Stamp a new 0–1 km revision so previously completed hours receive targeted repairs in missing-data case mode.
- Preserve provenance for regridded RAP native fields and for derived estimates; revisit old RAP conversions when source coverage changes. Add read-only direction diagnostics to `audit_storm_fields.py`.
- Keep existing multi-product selection for forecast models (`2dfld`/`prslev`, HRRR surface/pressure products, REFS member surface/pressure products) and do not invent missing REFS member storm-motion fields.

## v1.4.5 — November 1, 2026

See the v1.4.5 historical entry in this changelog for native-analysis catalog reconciliation, retention propagation, profile surface data, and normalized forecast-shear components.

## 1.4.4 — October 31, 2026
- Match both vertical layer orders of native USTM/VSTM to recover RRFS/REFS storm-motion barbs in incremental/case backfill.
- Retry SSH/SCP Python timeouts and fall back to a fresh connection after a stalled shared SSH control socket.
- Add a read-only storm-motion and shear numerical audit for cross-model 0–1 km shear discrepancies; retain existing native shear processing until source-unit behavior is verified from the case.
- Document that the current RAP `awp130pgrb` source may lack native 0–1 km shear, despite availability in a different RAP GRIB product; no fabricated analysis fields.

## 1.4.3 — October 31, 2026
- Automatically read existing `incremental_model_config.sh` for historical case worker settings, with CLI > CASE_* > cron-shared > built-in precedence and optional `--config-file` override.
- Add separate case worker overrides and a new shared cron/case configuration example with bounded, more parallel defaults. Ensure one OpenMP/BLAS thread per case subprocess and print the resolved worker sources.
- Preserve cron/case locks, catalogs and delta transfer ordering; no source-data or viewer changes.

## 1.4.2 — October 31, 2026
- Automatically coordinate historical-case execution and both cron launchers with a priority admission gate and shared cron execution gate. Active cron jobs finish; a waiting case takes priority; new cron runs skip and retry at their next scheduled interval. Preserve existing per-writer locks and concurrent model/verification cron behavior.
- Keep the execution lock inherited by active child converters after an unexpected coordinator exit. Add live process-lock tests and ship `case_cron_coordination.sh` alongside both updated cron launchers; no crontab modifications required.

## 1.4.1 — October 31, 2026: Native RAP 0–1 km bulk shear

- Add native RAP 0–1 km VUCSH/VVCSH ingestion, paired unit normalization, magnitude, and missing-field recovery.
- Update analysis field-source mapping and auditing; retain existing 0–6 km behavior.

## 1.4.0 (2026-10-31)

- Add a dependency-aware, bounded stage scheduler to historical `create_case.py` (default 2 stages, `--stage-workers 1–4`; 1 is the sequential fallback). Model, sounding, native analysis, observation and SPC stages can overlap when independent; alignment follows successful native source conversion. Forecast model publication is not launched if conversion fails.
- Serialize writers of the forecast catalog, sounding-profile catalog, native analysis catalog and aligned catalog. A failed prerequisite skips dependent stages but leaves independent work running; the case exits nonzero with clear status.
- Give each concurrent subprocess a separate mode-0600 log under `work/case_stage_logs`, avoiding interleaved stdout/stderr; the console shows stage start/finish/failure status.
- Allow the 15-minute model cron to overlap forecast-model processing with one sounding-profile process, awaiting both before advancing to the next cycle. `INCREMENTAL_PARALLEL_STAGES=0` restores serial order.
- Allow the hourly cron to overlap independent SPC and enabled observation report collection, awaiting both before any subsequent work. `HOURLY_PARALLEL_REPORTS=0` restores serial order.
- Add `--sounding-workers N` to case creation and `--hour-workers N` to the
  sounding utility (1–4, default 2), decoding distinct profile hours concurrently
  with separate work directories/chunks and per-hour progress timings.
- Reconcile missing forecast sounding hours after a model's conversion and
  original profile stage finish, avoiding missed newly available forecast hours
  while retaining single-writer profile-catalog publication.
- Preserve the existing cron locks, protection policies, incremental recovery, store transfer worker limits, SSH connection reuse, and catalog-after-data publication. New tests exercise concurrency, failure isolation, catalog dependencies and shell syntax. No new runtime dependencies, cron entries or data migrations.

## 1.3.9 (2026-10-31)

- Forward `create_case.py --publish-workers` to the aligned-analysis publisher, matching model, native verification, and profile stores. Keep a private SSH control socket alive throughout a case execution so sequential publishing stages can share connections.
- Reuse a private SSH control session across each observation/SPC report batch and create remote destination directories once per successful batch rather than per file. Atomic `.incoming` publication and per-hour failure isolation are retained.
- Keep the aligned-analysis catalog upload on the same SSH control session as its concurrent store transfers, strictly after every required store arrives.
- Add regression tests for worker forwarding, connection reuse, remote setup and catalog ordering. Cron commands and all data formats are unchanged.

## 1.3.8 (2026-10-31)

- Integrated native sounding-profile acquisition into `create_case.py`: all available processed forecast hours for selected model cycles and hourly RAP analysis profiles for the entire available verification window; no separate sounding backfill commands are needed.
- Added `--reprocess-analysis` to rebuild native verification (including previously incorrect RAP shear) without overwriting complete forecast model maps, alongside `--skip-soundings` opt-out and `--reprocess-soundings` to explicitly refresh already-completed profiles without forcing model/analysis map overwrites. `--mode all` also reprocesses profiles.
- Added bounded RAP hourly range processing (`--start-hour`/`--end-hour`) to the existing `sounding_profiles.py`; its profile stores and the profile catalog are published in one batch using configurable concurrent SSH/SCP workers.
- Case creation forwards `--publish-workers` to forecast, verification and sounding publication. The sounding utility also retries unpublished/stale local profile-store snapshots after interrupted publication; the profile catalog is updated only after successful transfers.
- Preserved both cron locks, protected-cycle retention, default dry-run, existing profile Zarr schema, map conversion semantics, and private site configuration.

# v1.3.7 — Severe-weather fields and efficient publishing (2026-10-31)

- Corrected native RAP 0–6 km bulk shear using paired magnitude-based gradient/difference detection; retained normalized U/V components and marked the conversion version so older RAP analyses are eligible for refresh.
- Added native Bunkers right-mover `USTM`/`VSTM` ingest for RAP analysis and supported forecast source products, with no synthesized replacement for missing records.
- Added `PUBLISH_WORKERS` / `--publish-workers` (1–6, default 3) for independent model, native analysis and aligned-analysis store transfers; existing processing worker pools are unchanged.
- Added opportunistic, private SSH multiplexing and bounded retry of transient SCP/SSH transport failures. Data-first/catalog-last publication, model changed-file snapshots, protected retention and cleanup ordering are preserved.
- Added regression checks for RAP unit normalization, source-field selection and transfer connection reuse.

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

## 1.4.20 — GFS v16/v17 native-grid forecast pipeline

- Added an independent, stdlib-only GFS converter with v16 `.idx` HTTP-range
  selection and local v17 `pres_a` GRIB2 message selection.
- GFS stores remain at 0.25° on a 601 × 281 regular lat/lon grid covering
  Alaska, Hawaii, Puerto Rico and the contiguous United States; no upscaling.
- Added F000–F240 on 3-hour increments, optional field inventory selection,
  native precipitation accumulation reconstruction, and retry/backfill.
- Added separate GFS catalogs, publication and seven-day retention with
  protected-cycle support. Existing analysis/observation ingestion unchanged.
- Added an opt-in GFS cron wrapper, example configuration, and regression tests.

## 1.4.21 — GFS inventory console logging

- Initialize GFS logging before protected-cycle parsing so INFO diagnostics are
  visible during direct `--inventory-only` invocations.
- Warn when no leads are eligible; suggest `--force-hours` for a repeated inventory.
- Add CLI subprocess regression coverage and explicit local testing steps.
- Leave conversion, cron processing, field inventories, output schema and source
  locations unchanged.


### Hourly RAP profile catalog compatibility (2026-11-03)
The shared sounding catalog publisher accepts RAP analysis profile stores at every UTC hour (00–23). Forecast model sounding stores retain their existing synoptic-hour validation. The same rules apply to remote catalog merge and expired-store cleanup; this corrects false publication failures for valid hourly RAP profiles without relaxing path restrictions.

## 1.4.29
- Optional supplemental GFS companion GRIB inventory for additional native sounding levels.
