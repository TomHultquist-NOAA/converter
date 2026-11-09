## v1.4.41 (2026-11-09)

Native 925-hPa specific humidity and dewpoint support for the GFS, AI, and regional model pipelines, with same-level thermodynamic derivation where required. Existing fields and source paths retained.

# EVU Model Data Converter

**Current release: v1.4.41** — shared precipitation verification and AI/GEFS model processing. The converter prepares forecast and verification
data for the EVU browser viewer and publishes static Zarr v2 grids,
sounding profile stores, and catalogs. It supports incremental collection,
protected historical cases, multiple configurable workers and controlled
remote publication.

New AI/GEFS workflow: see [`AI_MODEL_SET_INSTALL.md`](AI_MODEL_SET_INSTALL.md) for a dedicated cron, selective indexed source downloads, protected-case handling and deployment.

## Start here

- [`RELEASE_1_4_41.md`](RELEASE_1_4_41.md) — latest changes and compatibility
- [`DEPLOY_LINUX.md`](DEPLOY_LINUX.md) — upgrade checklist and safe rollback
- [`ARCHITECTURE_AND_AUDIT.md`](ARCHITECTURE_AND_AUDIT.md) — module map,
  avoidance of duplicate downloads, source-completeness rules and known risks
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

The ZIP includes source code, tests, generic examples and documentation at
its root. It deliberately excludes live credentials, protected-cycle state,
forecast outputs, remote transfer keys and service-specific configuration.
Use the site's already installed Python runtime, wgrib2 and accessible
processing/data directories; preserve the existing operational configuration.
See DEPLOY_LINUX.md. This package does not create a project source license;
choose one explicitly before publishing a public GitHub repository.

GEFS Mean now publishes native 0.25-degree available surface fields alongside native 0.5-degree pressure levels in a shared `catalog_gefs_mean.json` (see `AI_MODEL_SET_INSTALL.md`).

## Performance sampling policy (v1.4.40)

Performance sampling is **off by default** until the ingestion paths stabilize.
Normal cron processing and error logs remain active. Set `EVU_PERF_ENABLED=0`
in any already-deployed `converter_config.sh`, even if an older release set it
to 1. Use `python3 cleanup_performance_logs.py` to preview old performance
reports and `python3 cleanup_performance_logs.py --apply` to remove them.
The cleanup only targets performance report files and cannot delete forecast
GRIB/Zarr data or ordinary logs.

## GFS 250/1000-hPa field selection fix

The GFS field selector now uses its own extended field registry when selecting
GRIB records (`h250`, `u250`, `v250`, `t1000`, `h1000`). This resolves the
`KeyError: 'h250'` seen in cron and does not change RRFS/REFS field lists.
Unprocessed GFS hours remain eligible for retry in subsequent polls.
