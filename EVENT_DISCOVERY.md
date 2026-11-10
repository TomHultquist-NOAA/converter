# Experimental weather event discovery (v1.4.46)

This optional prototype screens existing, complete **local Zarr v2** forecast stores and the corresponding `catalog_MODEL.json` files to identify interesting events. It is **read-only**: it does not alter the viewer, rerun conversion, publish a catalog, contact an AI service, install packages, retain a cycle, start a cron task, or create a case study.

The scanner is compatible with **Python 3.6.8**, using only the standard library (`zlib`, `array`, `json`, `pathlib`, etc.). The prototype has been syntax-checked for Python 3.6 and tested with synthetic Zarr stores, but has **not** yet been validated on your live operational model catalogs. Treat its output as a preliminary list requiring review, not as a scientific verification product.

## Running a bounded first test

From the converter install directory, where the model catalog/data root is accessible:

```bash
python3 discover_weather_events.py \
  --data-root /path/to/your/zarr_processing/data \
  --output ./event_discovery_preview.json \
  --models rrfs,hrrr,refs_mean,gfs_v16,gfs_v17,aigfs,aigefs_mean,hgefs_mean,gefs_mean \
  --recent-cycles 1 --max-hour 48 --hour-step 12 \
  --max-frames-per-model 4 --max-events 20
```

Replace the example `--data-root` with the **directory containing** `catalog_rrfs.json`, `catalog_gfs_v16.json`, `catalog_aigfs.json`, etc., plus the corresponding `.zarr` directories. It may be a processing directory or a local, fully published mirror. The program will not download missing data; it simply records missing catalogs/fields as skipped. You can start smaller with `--models rrfs,gfs_v16,gfs_v17`.

The JSON preview contains ranked **group representatives** in `events`,
`event_groups` for the selected events (including individual scenes, measurements,
times, models and exact source records), and `candidate_group_summaries` for
**all** detected group candidates, including those not selected for the feed.
The summaries expose each group's score, candidate count, category/model mix,
maximum geographic span, selection status and selected rank. Each selected
`event_groups` entry includes `distinct_scene_count` and
`omitted_scene_count` so the 12-scene display cap is explicit. It also reports
category/model counts before and after selection, field availability, geographic
regions, and screening thresholds. `evu-event-discovery-preview-2` is an additive experimental
schema and is not yet used by the operational viewer. Nothing appears in the live viewer automatically. Only share or deploy the catalog after reviewing it.

## Geographic areas: always intersected with actual dataset coverage

Two model-group policies are built into the scanner:

- **RRFS/REFS/HRRR:** CONUS and surrounding waters/border regions only. There is **no** Alaska, Hawaii, or Puerto Rico event discovery from the regional group.
- **GFS and AI/GEFS:** Mainland North America (including Alaska, Canada, Mexico, and surrounding coastal waters),
a narrower Central American coastal corridor, the western Aleutians, Hawaii
and nearby waters, Puerto Rico and nearby waters, and Greenland. The broad
southern Caribbean offshore portion of the former mainland polygon has been
reduced after initial tests selected open-water CAPE hotspots southwest of
Puerto Rico. This does **not** include the entire Pacific/Atlantic ocean portions of their larger native grids.

Both policies are approximations for discovering useful events, not official political borders, national boundaries or exact coastlines. They are applied when searching coarse candidate locations **and** when refining actual extrema on the native grid. A reported event must also have real, finite forecast values at that grid cell and available forecast hour. The geographic policy cannot create coverage where the dataset has none.

For future expansion, an optional JSON file can override all three model-group profiles using `--geographic-regions event_regions.example.json`. The file has three keys: `rrfs`, `gfs`, `ai`. Each value is a nonempty array of named regions containing exactly one `bbox` (`[west,south,east,north]`) or `polygon` (array of `[lon,lat]` vertices). Polygons must not cross the international dateline; split those into separate regions instead. Invalid regions fail closed. A copy of the installed defaults is supplied as `event_regions.example.json`. The built-in defaults apply when no override is given.

## First detectors and limits

Initial objective screens are heavy 6-hour precipitation, 6-hour snowfall, strong surface gusts or derived 10-m wind, deep mean-sea-level lows, high CAPE, strong updraft helicity and three **same-grid, same-hour, same-initialization** GFSv17–v16 differences (MSLP, 500-hPa height and 850-hPa temperature). The algorithm ranks native-cell extrema inside the chosen geographic footprints, followed by experimental spatial-temporal grouping into weather situations
and a diversity-aware selection of representative groups. Groups now enforce maximum overall geographic diameter and time extent: nearby features cannot be joined by unbounded transitive chains. This intentionally may split a large evolving storm into several reviewable situations rather than falsely combining unrelated systems. The association is
heuristic: nearby detections can be related without being the *same* physical
storm. It is not a storm-tracking or verification algorithm. The simple thresholds are configurable with `--thresholds event_thresholds.example.json`.
A high-CAPE-only situation at latitudes below 18°N is additionally flagged in
`review_notes` because CAPE alone does not establish a severe-weather threat,
particularly in tropical environments. The scanner does NOT silently reject
those detections or alter the configured geographic masks. Starting with v1.4.47,
selected headline groups are limited to **one** tropical CAPE-only group by
default (`--max-tropical-cape-highlights 1`). This applies only to the
selection feed, not detection or geographic coverage. Use `0` to show none or
a larger number to retain more. The complete candidate-group summaries still
list every group, with `tropical_cape_only` and `selection_note` showing why a
group was or was not selected. CAPE-only headings now say 'High convective
instability' rather than implying that convection has been observed.

Distinct scenes are identified by **model, field, initialization cycle and
forecast lead**, avoiding accidental merging of different forecast runs with
the same valid time. The scene-cap review note now appears only when at least
one distinct scene was actually omitted.

This is **not** a climatological anomaly detector or an observation-based verification method. Accumulation screens require both endpoints of a complete six-hour interval; missing values or unmatched grids are skipped, never replaced by zero. Unsupported fields simply do not create events. The scanner may miss very small isolated maxima because the first pass is coarse; `--sample-km` controls that tradeoff. The prototype intentionally checks only selected hours and limited recent cycles to avoid competing with operational processing.

GFS 850-hPa temperature differences are screened **only above ground**: native
surface pressure must be at least 85000 Pa in BOTH versions at the candidate
cell. If either surface-pressure field/hour is unavailable, the corresponding
850-hPa temperature comparison is skipped rather than reporting a potentially
subsurface difference. The `coverage.gfs_difference` record includes counts
of surface-masked and skipped field-hours. This guard does not alter MSLP or
500-hPa height comparisons.

GFSv16/v17 cycles can arrive at different times. The scanner now seeks the most recent **common** cycle from the complete local catalogs, within a bounded age of the newest published cycle (default **72 hours**, configurable with `--comparison-lookback-hours`). This search is independent of the most recent cycles used for ordinary event discovery. The JSON includes `coverage.gfs_difference` diagnostics listing the comparison cycle and the reason if no comparison was possible. If no common cycle is available within the lookback, the scanner skips it instead of matching different initialization times. The GFS grids must also match.

The GEFS mean catalog can contain both 0.25-degree and 0.5-degree stores for one initialization; all matching stores are eligible, but no direct subtraction of different native resolutions is done at this stage. Other AI model disagreements and automated historical case preservation are future possibilities, not implemented features.

## Operational safeguards and future cloud migration

- No scheduler hook, publication, case-protection action or destructive access is installed.
- Each decompressed grid must fit the configured `--max-chunk-mb` guard. Input catalogs identify only existing local Zarr stores and complete chunk hours.
- A temporary JSON file is atomically renamed to the requested output path after scanning finishes.
- The local storage reader is isolated from event classification. A future AWS/GCP object-store or cloud-optimized-format adapter can implement the same catalog and grid read interface without rebuilding the geography rules or event schema.
- Keep this scanner at low priority and run it manually until event quality, performance, and catalog accuracy have been assessed against real forecast cycles.

For a follow-up review, provide the **new JSON preview and terminal output**.
Check `coverage` and `candidate_breakdown` before interpreting model differences.
A catalog listed as present may contain *zero eligible forecast hours* for the
current detector fields: this is now reported as `no_eligible_forecast_hours`,
with `catalog_fields`, `screening_fields` and `eligible_forecast_hours` included.
In particular, the first real-world run found one GFSv17 catalog entry but zero
screenable forecast hours, and therefore could not evaluate the GFS version
comparisons. This is **not evidence of no forecast differences**.

The grouping stage incurs negligible additional Zarr I/O. The `events` list now
contains one representative detection per selected situation with
`event_group_id`, `related_models`, `related_categories`, and
`related_detection_count`. `event_groups[*].scenes` keeps individual existing
valid-time/model/field views and data references (up to 12 per situation), so
future UI implementation need not invent or reconstruct meteorological values.
No case preservation, cron, or viewer integration is included.

**Next manual test (preserves your previous previews):**

```bash
python3 discover_weather_events.py \
  --data-root /path/to/your/zarr_processing/data \
  --output ./event_discovery_preview_v4.json \
  --recent-cycles 1 --max-hour 48 --hour-step 12 \
  --max-frames-per-model 4 --max-events 20
```

Compare the number of detected situations, the `candidate_group_summaries`
selection ranks and notes, and the GFS comparison mask counters against your v3
run. The output remains a manual preview; no viewer publication occurs.

## v1.4.46 review findings

The supplied v3 preview contains 171 candidate detections in 37 groups, with
20 groups selected. GFSv16/v17 comparison is now scanning a recent common
initialization and found 21 candidate disagreements. Several tropical CAPE-only
situations remain in the top 20; these are kept for scientific review with
context notes rather than excluded using an arbitrary additional geographic
cutoff. The scanner still does not establish climatological rarity, precipitation
areal coverage, or verified impacts. A future quality assessment may incorporate
land/coastal proximity, areal coverage, supporting convective fields and event
verification, but none is silently added in this release.

The groups are still heuristic proximity associations, not verified storm tracks.
No model files, cron jobs, viewer code or historical case protection are changed.

## v1.4.47 review findings

The supplied v4 preview found 171 detections, 38 candidate groups and 20 selected
groups. All four sampled GFS 850-hPa comparison field-hours had surface pressure
available, with none skipped. The diagnostic indicates mask evaluation was
possible; it is not a count of below-ground grid cells. V4 selected four
high-CAPE-only tropical groups, many close to one another in the southwest
Caribbean. The next preview limits those to one headline by default while
retaining all candidate evidence and leaving geographic limits unchanged.
Grouping, field thresholds and operational data processing are unchanged.


## v1.4.48: optional historical watches, warnings and advisories

The optional standard-library script `enrich_weather_event_alerts.py` takes an existing
experimental preview and adds `alert_context` for selected situations, using time-and-point
queries from the Iowa Environmental Mesonet VTEC watch/warning/advisory and storm-based
warning services. For the latter, precise storm-based geometries or point buffers are used
by the upstream service; ordinary VTEC county/zone results cover larger areas and are
**not** precise warning polygons. Representative event points/time samples are not a
complete spatial intersection. This is supplemental US-centric context, not observed
storm impacts, and should never be a required detection condition.

For manual enrichment:

```sh
python3 enrich_weather_event_alerts.py --input event_discovery_preview_v5.json \
  --output event_discovery_preview_v5_alerts.json --samples 2
```

The scanner still works offline without alerts. A scheduled invocation first runs the scanner,
validates data coverage and cycle recency, then optionally queries IEM with a conservative
one-time sample per situation. It records `matched`, `forecast_period_upcoming`,
`no_matching_us_alerts`, `unavailable`, or partial coverage with exact source links.
Future forecast valid times may acquire new watches/warnings later. IEM currently covers US
VTEC products; this is not a Canadian alert archive and never asserts that unmatched
locations are safe. The experimental viewer displays matching records when provided.

Cron, expected deployment paths, opt-in publication safety, and example configuration are in
`WEATHER_EVENTS_SCHEDULING.md`.

## Scheduled scan depth (v1.4.49+)

The original 48/12/4/20 command above is a low-resource diagnostic example. For regular discovery the independent scheduled wrapper defaults to one cycle per model, through F072 at six-hour steps (12 frames per model cycle) and up to 15 selected situations. All five arguments, including recent cycles, are configurable in `weather_events_cron_config.sh`. See `WEATHER_EVENTS_SCHEDULING.md`.

## Headline focus (v1.4.50)
The default `--max-events 15` limits displayed highlights. A soft ranking penalty reduces nearby same-category repetitions while preserving all candidates. This does not change the scan cadence, thresholds, masks, or grouped situation definitions.
