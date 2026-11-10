**Weather Events v1.4.50:** Recommended scheduled headline cap is 15. A soft geographic/time similarity penalty reduces repeated same-category headlines; all candidate records are retained. Update an existing `weather_events_cron_config.sh` manually to set `WEATHER_EVENT_MAX_EVENTS=15`.

## v1.4.48 (2026-11-10)

Experimental Weather Events now includes balanced scene selection, optional IEM historical watch/warning/advisory context (`enrich_weather_event_alerts.py`), and independent validated cron publication (`run_weather_events_cron.sh`, `weather_event_publish.py`). Default cron config is preview-only until an explicit destination is set. See `WEATHER_EVENTS_SCHEDULING.md`. No operational converter or retention behavior changes.

## v1.4.47 (2026-11-10)

The experimental, read-only event scanner now masks GFS 850-hPa temperature disagreements below the surface, audits all candidate situation groups (including those not selected), and reports context for tropical high-CAPE-only situations. It also uses more accurate precipitation/snowfall group titles. No operational conversion, cron, retention, publication, or viewer changes. See [`EVENT_DISCOVERY.md`](EVENT_DISCOVERY.md).

## v1.4.45 (2026-11-10)

Experimental event-discovery improvements: prevent unbounded chained event grouping and find the latest compatible GFSv16/v17 common cycle within a configurable lookback. Read-only, manual-run scanner; no operational changes. See [`EVENT_DISCOVERY.md`](EVENT_DISCOVERY.md).

## v1.4.44 (2026-11-10)

Optional **experimental event discovery**: read-only, Python 3.6.8-compatible scanning of existing Zarr v2 stores, with model-specific North America / CONUS geographic masks and a local JSON preview. See [`EVENT_DISCOVERY.md`](EVENT_DISCOVERY.md). No cron, viewer, case, or converter behavior changes.

## v1.4.42 (2026-11-09)

Optional instrumentation now covers the previously missing AI model poller and detailed AI/GFS hour processing. The read-only `summarize_performance_logs.py` reports which pipelines produced completed monitoring reports; retention and protected-cycle behavior are unchanged. Read `PERFORMANCE_MONITORING.md` and `CHANGELOG.md`.

## v1.4.41 (2026-11-09)

Native 925-hPa specific humidity and dewpoint support for the GFS, AI, and regional model pipelines, with same-level thermodynamic derivation where required. Existing fields and source paths retained.

# EVU Model Data Converter

**Current release: v1.4.48** — shared precipitation verification and AI/GEFS model processing. The converter prepares forecast and verification
data for the EVU browser viewer and publishes static Zarr v2 grids,
sounding profile stores, and catalogs. It supports incremental collection,
protected historical cases, multiple configurable workers and controlled
remote publication.

New AI/GEFS workflow: see [`AI_MODEL_SET_INSTALL.md`](AI_MODEL_SET_INSTALL.md) for a dedicated cron, selective indexed source downloads, protected-case handling and deployment.

## Start here

- [`RELEASE_1_4_46.md`](RELEASE_1_4_46.md) — latest changes and compatibility
- [`DEPLOY_LINUX.md`](DEPLOY_LINUX.md) — upgrade checklist and safe rollback
- [`PERFORMANCE_MONITORING.md`](PERFORMANCE_MONITORING.md) — transfer throughput,
  CPU, memory and staged jobs
- [`CASE_STUDIES.md`](CASE_STUDIES.md) — protected historical case workflow
- [`SOUNDING_PROFILES.md`](SOUNDING_PROFILES.md) — soundings and native levels
- [`DETAILED_REFERENCE_LEGACY.md`](DETAILED_REFERENCE_LEGACY.md) — older
  troubleshooting notes retained for historical context
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — tests and regression safeguards

## Model and analysis handling

The regional pipeline handles RRFS, HRRR, five REFS members and REFS mean.
The GFS pipeline handles v16/v17 fields, native upper-air/sounding inventory
and the regionalized GFS analysis alignment. Verifying sources include MRMS
instantaneous precipitation rate, CCPA/Stage IV/MRMS accumulation fallback,
NOHRSC snowfall, RAP, RTMA and URMA, with actual availability determining
which products can be displayed. Analysis windows must be complete and
internally consistent; missing sources are not replaced by fabricated zero.

A GFS GRIB record shared by a forecast map field and a sounding is selected
once per processing hour. Finished leads are skipped using native status and
manifests, subject to explicit force/retry settings. Case and incremental
operations coordinate using the existing lock files; protected cycles are
excluded from standard seven-day cleanup.

Worker ceilings have been removed at the user's request. Values must remain
positive integers and the server operator is responsible for monitoring
resource pressure and publication throughput. No mandatory new runtime
packages were introduced in this update.

## Deployment and security

The ZIP includes operational source code, generic examples and documentation at
its root. Internal tests, test fixtures, and generated logs are excluded, as are live credentials, protected-cycle state,
forecast outputs, remote transfer keys and service-specific configuration.
Use the site's already installed Python runtime, wgrib2 and accessible
processing/data directories; preserve the existing operational configuration.
See DEPLOY_LINUX.md. This package does not create a project source license;
choose one explicitly before publishing a public GitHub repository.

GEFS Mean now publishes native 0.25-degree available surface fields alongside native 0.5-degree pressure levels in a shared `catalog_gefs_mean.json` (see `AI_MODEL_SET_INSTALL.md`).

## Performance sampling policy (v1.4.40)

Performance sampling remains **opt-in** (`EVU_PERF_ENABLED=1`); new AI/GFS coverage is available when intentionally enabled.
Normal cron processing and error logs remain active. For ongoing performance diagnosis, preserve `EVU_PERF_ENABLED=1`
in your existing `converter_config.sh` (or set it intentionally to 1). Use `python3 cleanup_performance_logs.py` to preview old performance
reports and `python3 cleanup_performance_logs.py --apply` to remove them.
The cleanup only targets performance report files and cannot delete forecast
GRIB/Zarr data or ordinary logs.

## GFS 250/1000-hPa field selection fix

The GFS field selector now uses its own extended field registry when selecting
GRIB records (`h250`, `u250`, `v250`, `t1000`, `h1000`). This resolves the
`KeyError: 'h250'` seen in cron and does not change RRFS/REFS field lists.
Unprocessed GFS hours remain eligible for retry in subsequent polls.

Experimental event discovery v1.4.47: use `--max-tropical-cape-highlights`
to limit tropical high-CAPE-only groups in the selected preview without
removing them from the full candidate audit. See EVENT_DISCOVERY.md.

**Weather Events scheduled scan (v1.4.49):** configure all five sampling limits in `weather_events_cron_config.sh` (`WEATHER_EVENT_RECENT_CYCLES`, `MAX_HOUR`, `HOUR_STEP`, `MAX_FRAMES_PER_MODEL`, `MAX_EVENTS`). Defaults now screen through F072 at 6-hour steps; see `WEATHER_EVENTS_SCHEDULING.md`.
