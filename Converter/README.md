# OMD EVU Multi-Source GRIB2-to-Zarr Processor

WCOSS2 processing and publishing workflow for the **Office of Modeling and Development (OMD), Evaluation and Verification Unit (EVU)** model viewer.

The processor selects meteorological fields from NOAA GRIB2 inventories, downloads only needed byte ranges, decodes them with `wgrib2`, creates browser-compatible **Zarr v2** stores, and publishes completed datasets and model catalogs to RZDM. It supports concurrent forecast-hour workers, incremental resumption, automated six-hourly operation, and configurable retention on both servers.

**Release:** Version 5.1 (unified eight-source defaults; clean standalone three-file edition). The Python processor includes the reusable RRFS decoding/Zarr helpers internally, so the older `rrfs_to_zarr_wcoss2_auto_publish.py` is **not required by this multi-source script**.

## Package contents

| File | Purpose |
| --- | --- |
| `multi_model_to_zarr_wcoss2.py` | Main processor, Zarr conversion, publishing and retention |
| `run_multimodel_cron.sh` | Automatic cycle selection, catch-up, locking and logging |
| `converter_cron_env.sh` | Minimal WCOSS2 module/environment setup for cron |
| `README.md` | Deployment, usage and maintenance instructions |

No test scripts or development artifacts are included in the deployment archive.

## Supported sources

| Source ID | Input | Forecast hours |
| --- | --- | --- |
| `rrfs` | Deterministic RRFS | F000–F084 |
| `refs_mean` | REFS ensemble mean | F001–F060 |
| `refs_m001` through `refs_m005` | Five REFS ensemble members | F000–F060 |
| `hrrr` | HRRR surface and pressure products | F000–F048 |

The cron wrapper now defaults to **all eight forecast sources**, including RRFS: `MODELS="${MODELS:-all}"`. This matches the current nine-panel viewer. **Disable the old standalone RRFS conversion job** before using the unified processor against the same store.

Fields include the existing surface and pressure variables, **10-m U/V wind components**, **derived 10-m wind speed**, **gust**, and HRRR **FROZR** (frozen rain) and **FRZR** (freezing rain). Input `.idx` inventories determine which fields are actually available for each product and forecast hour; missing fields are not fabricated. Accumulation fields are handled to avoid treating incremental precipitation as forecast-total precipitation.

## Requirements

- WCOSS2 Python 3.8+ with the **standard library only** (no pip/conda/Numpy/xarray/Zarr installation needed).
- `wgrib2` executable and sufficient CPU, memory, scratch space and I/O permissions.
- `bash`, `flock`, `ssh`, `scp`, `tar`, and noninteractive SSH authorization to RZDM for unattended publishing.
- On RZDM: write access to the destination directory and `python3` for remote retention. The remote cleanup code is compatible with Python 3.6.

**Important:** Confirm your WCOSS2 compute and cron scheduling policies before running high worker counts. Although `--workers` supports 1–20, start with about 8 and adjust for your resource allocation.

## Installation and configured paths

Extract the files into:

```text
/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/
```

The cron wrapper is already configured with these paths:

```bash
BASE_DIR="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing"
OUTPUT_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr"
WORK_ROOT="/lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
WGRIB2_BIN="${WGRIB2_BIN:-$(command -v wgrib2 || true)}"
```

It also exports `OMP_NUM_THREADS=1`, `OMP_THREAD_LIMIT=1`, `MKL_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, and `NUMEXPR_NUM_THREADS=1` to prevent OpenMP thread exhaustion while multiple Python workers invoke `wgrib2`. The Python script applies the same settings when run directly.

The default RZDM publish target is:

```text
thultquist@rzdm.ncep.noaa.gov:
/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data
```

You can override it with `--publish-host` and `--publish-dir` when running the Python program manually.

**Upgrade caution:** Wait until any active processing job completes before replacing the scripts. Retain existing `zarr/`, `work/`, and `logs/` directories, and preserve changes you made to the cron wrapper.

## Checking field availability

Query `.idx` inventories without downloading or processing GRIB data:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261009 --cycle 00 \
  --models hrrr,refs_mean,refs_m001 \
  --hours 1 --inventory-only
```

This is useful for confirming HRRR FROZR/FRZR, 10-m wind fields, and source-specific field availability.

## Running manually

Process or resume a known cycle (uses your existing local output/work directories):

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261009 --cycle 06 \
  --models all \
  --workers 8 \
  --wgrib2 /apps/ops/prod/libs/intel/19.1.3.304/cray-mpich/8.1.4/wgrib2/3.8.0/bin/wgrib2 \
  --output-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr \
  --work-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work \
  --retention-days 7
```

To backfill a different run, change `--date` and `--cycle`. To process a subset, add `--hours 0-12` (the REFS mean naturally begins at F001). **Re-running skips completed forecast hours.** Add `--overwrite-hours` only when you explicitly need to replace already-completed data.

To rebuild/publish existing output without re-downloading GRIB, see `--catalog-only` and `--publish-only` in `python3 multi_model_to_zarr_wcoss2.py --help`.

## Automatic six-hourly processing

The cron wrapper accepts **no positional arguments**. It selects the latest eligible UTC cycle, retries the previous cycle, manages its own lock file, and writes timestamped logs under `BASE_DIR/logs/`.

Run it manually first:

```bash
/bin/bash /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/run_multimodel_cron.sh
```

Recommended crontab (assuming the cron host uses UTC):

```cron
SHELL=/bin/bash
USERSHELL=/bin/bash
MAILTO=""
7 4,10,16,22 * * * /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/run_multimodel_cron.sh >> /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/cron_errors.log 2>&1
```

Run `chmod 750 run_multimodel_cron.sh` after installing. The wrapper sources
`converter_cron_env.sh` (not the complete `.bashrc`); it initializes the
`module` command, loads compiler/MPI, `wgrib2/3.8.0` and `python/3.10.4`,
then resolves executable paths from the loaded module environment.
If your site's modules initialization file differs, set `MODULE_INIT_FILE`
near the top of `converter_cron_env.sh` or in the crontab. Do not use
`bash -l` or other login-shell workarounds.

The installed module versions follow the supplied interactive `.bashrc`, but
may need adjustment for changes on WCOSS2. Test the environment independently:

```bash
cd /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing
/bin/bash -c 'set -e; source ./converter_cron_env.sh; command -v python3; python3 --version; command -v wgrib2; wgrib2 -version'
```

Note: WCOSS2 guidance says that cron-launched heavy computation should be
submitted to PBS; this version deliberately does not change execution mode.


The wrapper logs normal processing internally; `cron_errors.log` can capture unexpected startup output. If the host is not set to UTC, adapt the schedule accordingly.

Key settings at the top of `run_multimodel_cron.sh`:

| Variable | Default | Description |
| --- | --- | --- |
| `WORKERS` | `8` | Parallel forecast-hour processes (1–20) |
| `CYCLE_LAG_HOURS` | `3.5` | Initialization must be at least 3 h 30 min old before automatic selection |
| `CATCHUP_CYCLES` | `1` | Revisit one earlier six-hour cycle (0–3 supported) |
| `RETENTION_DAYS` | `7` | Keep cycles on both WCOSS2 and RZDM |
| `MODELS` | `all` (all eight sources) | RRFS, REFS mean, REFS members 1–5, HRRR; can be narrowed to one or several sources |
| `PYTHON_BIN`, `WGRIB2_BIN` | WCOSS2 values above | Executables; use absolute paths for cron where needed |

The corresponding direct-Python auto mode is:

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date auto --cycle auto --lag-hours 3.5 \
  --models hrrr,refs_mean --workers 8 \
  --output-root ./zarr --work-root ./work
```

**Cycle selection:** With a 3.5-hour eligibility delay, 04:00, 10:00, 16:00, and 22:00 UTC choose respectively the 00, 06, 12, and 18 UTC model cycles. The delay does not limit explicitly specified historical cycles.

## Data retention and safety

`--retention-days 7` retains model cycles whose initialization times are within the last seven days on **both** WCOSS2 and RZDM. It compares initialization timestamps, **not file modification times**.

Locally, recognized expired Zarr stores, manifests, summaries, dated scratch directories, optional retained publishing archives, and optionally named cron logs are removed. Remotely, only recognized expired model Zarr directories are removed; expired entries in recognized catalogs are pruned. Unrelated files, source code, symlinks, undated legacy scratch folders, and viewer assets are not targets.

**Always preview deletion when first configuring or changing paths:**

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date auto --cycle auto \
  --output-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr \
  --work-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work \
  --retention-days 7 --cleanup-only --cleanup-dry-run
```

Use `--no-local-cleanup` or `--no-remote-cleanup` to disable one side. To change retention on scheduled runs, set `RETENTION_DAYS` in the wrapper. Historical backfills older than the retention window may be immediately excluded or deleted; increase retention before backfilling those dates.

## RRFS and the current model viewer

**Important for users upgrading from the seven-source cron wrapper:** earlier versions defaulted to processing only REFS members, REFS mean, and HRRR. Version 5.1 enables RRFS in the six-hourly workflow too. That is why `catalog_rrfs.json` and recent `rrfs_YYYYMMDD_CC00.zarr/` folders were previously absent even though the other seven sources were successful.

The updated viewer (V7.18+) reads RRFS from **`data/catalog_rrfs.json`**, not the legacy **`data/catalog.json`**. The unified publisher creates and updates the former without deleting or overwriting the latter. Make sure you deploy both updated configurations and publish at least one RRFS cycle. Until RRFS has been processed for a selected cycle, that panel correctly displays *No matching cycle*.

To backfill an existing initialization with **RRFS only** (the new script will skip completed forecast hours):

```bash
python3 multi_model_to_zarr_wcoss2.py \
  --date 20261009 --cycle 06 --models rrfs --workers 8 \
  --wgrib2 /apps/ops/prod/libs/intel/19.1.3.304/cray-mpich/8.1.4/wgrib2/3.8.0/bin/wgrib2 \
  --output-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/zarr \
  --work-root /lfs/h2/emc/vpppg/noscrub/thomas.hultquist/zarr_processing/work \
  --retention-days 7
```

Change `--date` / `--cycle` for the backfill needed. The cron wrapper automatically revisits only the newest cycle and the configured number of previous cycles (`CATCHUP_CYCLES`, default 1), **not** all past seven days. Backfill older cycles explicitly if you want them in the viewer's full seven-day cycle list.

## Outputs and troubleshooting

- Local stores: `OUTPUT_ROOT/MODEL_YYYYMMDD_CC00.zarr/`.
- Model-specific catalogs: `catalog_rrfs.json`, `catalog_refs_mean.json`, `catalog_refs_m001.json`, etc.; these **do not overwrite** the legacy `catalog.json`.
- Local run summaries: `run_summary_YYYYMMDD_CC.json`.
- Logs: `BASE_DIR/logs/multisource_YYYYMMDD_HHMMSS.log`.
- Completed hours are recorded so subsequent invocations can resume without reprocessing those hours.

**`wgrib2` aborts with OMP Error #34 / SIGABRT:** Verify all five OpenMP environment settings above are active, and reduce `WORKERS` if compute-node resources are limited.

**Files not yet available (HTTP 404):** The processor logs absent products/fields separately from genuine decoding or download failures. Later invocations can retry hours still unprocessed. If an hour was completed using only some products, use `--overwrite-hours` to revisit it after late products arrive.

**Publishing or cleanup error:** Inspect the detailed log and `publish_errors` in the JSON run summary. Confirm SSH/SCP, remote write permissions, free space, and RZDM `python3`. A successful local conversion does not necessarily imply the dataset has reached RZDM; `--publish-only` can be used to republish existing stores.

**No terminal output from cron wrapper:** It redirects normal output to its internal timestamped log, even when launched interactively. Use `tail -f` on the current file in `logs/` to watch progress.

This deployment package intentionally omits automated test scripts; regression tests are run separately during development.
