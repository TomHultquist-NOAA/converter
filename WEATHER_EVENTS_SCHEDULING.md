# Weather Events: optional scheduled scans, historical alert context, and publication

This is a **separate experimental** discovery/publication workflow. It reads published local
model catalog and Zarr chunks. It never calls operational converter scripts, reprocesses
forecasts, preserves cycles, changes existing converter cron, or updates the source Zarr.
The viewer reads only a validated JSON feed (when published).

## One-time setup

1. Deploy the entire converter release in a trusted directory on the Python 3.6.8 server.
2. Copy `weather_events_cron_config.example.sh` to `weather_events_cron_config.sh`.
3. Set `WEATHER_EVENT_DATA_ROOT` to the **actual local directory** that already contains
   `catalog_rrfs.json`, `catalog_gfs_v16.json`, etc. together with their `.zarr` stores.
4. Set `WEATHER_EVENT_WORK_DIR` to a writable directory for JSON previews and the lock.
   Do **not** put this working directory inside a public web directory.
5. Keep `WEATHER_EVENT_PUBLISH_TARGET=''` for your first run. From the converter directory:

```sh
./run_weather_events_cron.sh
```

The result is preview-only until a publish destination is explicitly configured. The
publisher requires at least 5 scanned models, 3 selected groups, fresh scan output, and
selected forecast initialization cycles within 36 hours. Failures leave the previous
published JSON unchanged. There is no requirement for every model to have a qualifying
weather event. The publisher uses a nonblocking `fcntl.flock` lock and `nice -n 10`
when available. One worker runs the scanner and one runs optional alert lookup. No
additional Python packages are required.

## Forecast sampling configuration (v1.4.49+)

`weather_events_cron_config.sh` is the source of truth for the scheduled scan;
`run_weather_events_cron.sh` passes its settings to `weather_event_publish.py`,
which passes them to `discover_weather_events.py`. The standard settings are:

```sh
WEATHER_EVENT_RECENT_CYCLES=1
WEATHER_EVENT_MAX_HOUR=72
WEATHER_EVENT_HOUR_STEP=6
WEATHER_EVENT_MAX_FRAMES_PER_MODEL=12
WEATHER_EVENT_MAX_EVENTS=15
```

This screens forecast leads F006, F012, ... F072, limited to hours actually
available from each published model. `WEATHER_EVENT_RECENT_CYCLES=1` means the
most recent initialization available for **each** model; GFS differences
still use the newest matching GFSv16/GFSv17 cycle within the separate bounded
lookback. `WEATHER_EVENT_MAX_FRAMES_PER_MODEL` caps the first available sampled
leads, so raising max-hour alone does not expand coverage unless this cap is
also raised. `WEATHER_EVENT_MAX_EVENTS` controls the *selected* situations in
the public feed; all candidates remain summarized separately. Selection is
also bounded by scanner per-category limits and may yield fewer highlights.

The more thorough setting may substantially increase compressed reads/CPU
versus the 48-hour, 12-hour-step trial. Check the first run before putting it
on a frequent schedule, particularly if conversion overlaps. A lighter
alternative is 48 / 6 / 8 / 15 (max hour / step / frames / events).

**Existing installations:** copying a new example file will NOT change your
existing `weather_events_cron_config.sh`. Update the values in that installed
file manually, including the new `WEATHER_EVENT_RECENT_CYCLES`. No operational
converter configuration or cron entry is altered by this release.

## Add a publication destination (when you are satisfied with the preview)

For a viewer directory mounted on the same machine:

```sh
WEATHER_EVENT_PUBLISH_TARGET='local:/absolute/web/viewer/data/weather_events.json'
```

For a separate SSH destination with noninteractive SSH keys:

```sh
WEATHER_EVENT_PUBLISH_TARGET='ssh:user@hostname:/absolute/web/viewer/data/weather_events.json'
```

Publication uses a temporary file in the destination directory, permissions `0644`,
then an atomic rename (local or over SSH). The target `data/` directory must exist,
be writable by the selected account, and be readable by the web server. SSH mode
requires `scp`, `ssh`, and `BatchMode=yes` key-based authentication; no passwords
or tokens are embedded in the config. If upload or validation fails, the previous feed
remains in place. All feed assets are JSON; no model data is copied.

## Cron entry (UTC)

Install in the **converter host** crontab after the conversion jobs have normally
finished. The example below is staggered from 04/10/16/22 UTC converter cycles;
actual local processing duration may vary, but the scanner does not wait on them.

```crontab
CRON_TZ=UTC
40 5,11,17,23 * * * /bin/bash /absolute/path/to/converter/run_weather_events_cron.sh >> /absolute/path/to/weather_events_work/weather_events_cron.log 2>&1
```

`CRON_TZ` works on supporting cron implementations; confirm server time-zone rules
before enabling. If conversion is still running, delay this schedule rather than
running heavy reads during ingestion. The scanner has its own lock so two event
runs cannot overlap. Normal source retention continues; historical event links
may become unavailable when their model cycle expires.

## Watch/warning/advisory enrichment

By default, selected groups receive a bounded IEM VTEC county/zone and storm-based
warning point lookup for **one representative valid time** per group. This is a
US-centered historical alert reference, not a comprehensive map of all alerts
covering an entire modeled storm region. An archived match means a warning/watch/
advisory product was associated with that point/time; it does not verify damage,
infrastructure impact or any observed weather. Future forecast situations may
not yet have any warnings issued. County/zone alert coverage is much larger
than storm-based polygon coverage. Non-US alert providers are not yet included.

If the IEM source is temporarily unavailable, the feed explicitly displays the
unavailable status, rather than reporting zero warnings. Set `WEATHER_EVENT_STRICT_ALERTS=1`
if you prefer the existing feed to remain unchanged whenever any lookup fails.
Set `WEATHER_EVENT_SKIP_ALERTS=1` to produce forecasts offline and mark all
alert context as not checked. The alert endpoint uses standard HTTPS with a short
timeout; the scanner itself is otherwise offline.

## Checks and troubleshooting

- Run `python3 weather_event_publish.py --help` for limits. It includes guards
  on JSON schema, model coverage, event counts, creation time, and cycle age.
- Run `python3 enrich_weather_event_alerts.py --help` for manual higher-sampling
  lookups, e.g. reviewing a case with 2 or 3 valid times.
- The browser supports both enriched feeds and older scans without alert context.
- Expired forecast cycles are marked unavailable in the viewer; no automatic
  backfilling or preservation occurs.
- The public browser is an experimental forecasting exploration aid, not an
  emergency alerting system or an operational decision-support product.

Data provenance:
- IEM API: https://mesonet.agron.iastate.edu/api/
- VTEC point: https://mesonet.agron.iastate.edu/json/vtec_events_bypoint.py?help=
- Storm-based warning point: https://mesonet.agron.iastate.edu/json/sbw_by_point.py?help=

## More focused headline list (v1.4.50)
Set `WEATHER_EVENT_MAX_EVENTS=15` in your **existing** `weather_events_cron_config.sh`; the example and fallback defaults are now 15 but will not override your current configuration. This controls only the selected groups in the public feed, not candidate detection or WWA correlation. Headline ranking mildly discounts additional nearby situations of the same primary weather category within about 450 km and overlapping/nearby times; no event is removed from the candidate group summaries.
