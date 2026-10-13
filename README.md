# OMD EVU Model Viewer

**Version 7.43.1 · October 13, 2026**

A browser-based, multi-panel weather-model viewer for the Office of Modeling and Development (OMD), Evaluation and Verification Unit (EVU). It displays forecast fields from hosted Zarr v2 datasets using MapLibre GL JS and autumnplot-gl. Map geography is served from local GeoJSON files; plots are rendered in the browser.

## Viewer capabilities

- Compare multiple models and ensemble members in a synchronized grid (default: 3 × 3, configurable up to 20 panels).
- Select a forecast field, model initialization cycle, and forecast hour; step or animate through available forecast times.
- Pan and zoom synchronized maps in **2D** or **Globe** mode. Each mode remembers its own center and zoom while the page is open. On mouse-equipped devices, the glowing master cursor uses a native cursor graphic (no animation-frame trailing); matching geographic crosshairs remain synchronized across the other panels.
- Display local state, country, coastline, lake, and zoom-dependent county boundaries. Expanded panels adjust county visibility to account for their larger geographic viewport, without altering multi-panel thresholds.
- Use light or dark mode, expand an individual panel, and inspect data under the cursor. On desktop, switch the controls between the top toolbar and a left sidebar (default for new visitors) using the icon beside the theme button; the layout choice is saved in the browser.
- Save an individual panel or the full comparison as a PNG in **16:9** or **4:3**, with **Fill** or **Fit** framing. Exports prepare the applicable geographic detail before capture.
- Toggle **Contours** on supported continuous fields (temperature, moisture, winds, precipitation, snow amounts, and reflectivity). Contours are rendered in the current map projection for 2D and Globe views, and included in PNG exports. While panning, zooming, or rotating, the contour overlay is temporarily hidden and no CPU-intensive contour calculations are performed; it redraws at full quality when movement has stopped. Conventional field-specific intervals adapt to map zoom, data range, and panel dimensions; continuous, interpolated paths have readable, rotated, non-overlapping labels even in normal-size panels. Categorical fields have no contour control. The choice is remembered by the browser; contour lines are off by default.
- Compare **1-hour precipitation** or **accumulated precipitation since the selected model cycle initialization**. The model F001 one-hour amount equals its F001 cumulative total (no F000 file required); at F002 and beyond the one-hour amount is derived from consecutive model running totals; the Analysis panel uses MRMS 1-hour QPE directly or adds consecutive hourly MRMS QPE valid times for the matching cycle-to-date total. Missing MRMS hours prevent displaying an incomplete running total. The MRMS sum is computed on demand in the browser, which can be slow or memory-intensive for long forecast leads at native MRMS grid resolution.
- Prefetch upcoming forecast data, with bounded browser caches and adaptive network use.
- Use the viewer on desktop and mobile, including sequential mobile map initialization and WebGL resource recovery.

## Desktop control placement

On desktop-width browsers (over 800 px), the layout button beside the light/dark switch toggles between **Top** and **Left** (default for first-time visitors). The chosen placement is saved using browser `localStorage` and restored on later visits, overriding the first-visit default. When left placement is selected, the navigation tips, live panel status counts, and autumnplot-gl attribution also move into the sidebar. The left-side controls scroll independently when the window is short. The existing mobile control layout is unaffected and does not display this toggle.

## Default panel arrangement

| | Column 1 | Column 2 | Column 3 |
| --- | --- | --- | --- |
| **Row 1** | RRFS | HRRR | REFS-1 |
| **Row 2** | REFS-2 | REFS-3 | REFS-4 |
| **Row 3** | REFS-5 | REFS-mean | Analysis (verification) |

The Analysis panel uses valid-time-matched verification fields from `data/catalog_analysis.json`. For common near-surface fields, it uses preliminary RTMA while the corresponding URMA valid hour is not yet available, and automatically prefers URMA when both are present. It labels RTMA explicitly as preliminary. REFS mean categorical fields are displayed as continuous weighted-mean fractions rather than binary flags. Missing or delayed verifying data is identified in the panel; it is not replaced by an older hour. MRMS is ingested at full native resolution (stride 1) by default, which requires more memory and bandwidth. The **Accumulated precipitation** Analysis panel is computed from *1-hour MRMS QPE ending at hours F001…Fxxx*, excluding the initialization hour itself; it is comparable to cycle-to-date model totals, not an arbitrary UTC-day accumulation. If any hourly QPE is absent, the Analysis panel explicitly reports the missing input and does not show a misleading partial sum. MRMS backfills become available on page reload after the verification catalog updates.

## Deployment and updates

1. Copy the ZIP contents **directly into the web viewer directory**, retaining the included directory structure. There should be no additional enclosing folder.
2. **Preserve `vendor/`**, including the MapLibre CSP worker and autumnplot-gl distribution. On a new installation, use `vendor_dependencies.py` to obtain the required third-party files.
3. **Preserve operational data** in `data/`: `catalog*.json` and referenced `.zarr` directories. Those are provided by the separate GRIB2-to-Zarr processing and publishing workflow, not by this ZIP.
4. Back up any locally customized `config.json` and merge settings as appropriate before replacing it.
5. If upgrading an earlier installation, you can delete the obsolete `build_catalog.py` from the web directory. Extracting a new ZIP does not remove files left by older releases.
6. Hard-refresh the browser after deployment to avoid cached viewer scripts or styles.

Serve the viewer over HTTP(S) rather than opening `index.html` as a local file. Persistent compressed-data caching requires a browser environment that supports CacheStorage (typically HTTPS or localhost).

## Main files

| Path | Purpose |
| --- | --- |
| `index.html` | Viewer entry point |
| `js/viewer.js` | Rendering, navigation, data access, prefetching, and PNG exports |
| `css/styles.css`, `css/cursor-master-glow.svg` | Responsive layout, visual styling, and the native master-pointer graphic |
| `config.json` | Model sources, panels, display, cache, and export options |
| `data/map_*.geojson` | Geographic boundaries and fills |
| `diagnostics.html` | WebGL, MapLibre, worker, and multi-panel diagnostics |
| `vendor_dependencies.py` | Third-party browser library setup helper |

The verification acquisition, conversion, and publishing scripts are distributed separately from the web viewer. No WCOSS2 converter update is needed for v7.43; both MRMS precipitation modes use the already published native-hourly `mrms_apcp` Zarr stores. A future server-side prefix-sum product could accelerate very long lead-time totals.

## Configuration

Edit `config.json` to change model catalog locations, panel order, default field, map options, and resource limits. Frequently useful settings under `display` include:

| Setting | Purpose |
| --- | --- |
| `maxActiveMaps` | Maximum active MapLibre maps (default 9) |
| `countyMinZoom` / `globeCountyMinZoom` | County visibility thresholds in 2D / Globe (4.25 / 4.30) |
| `countyPreloadLeadZoom` | Preload county geography ahead of visibility (0.35) |
| Expanded panel county threshold | Automatically delays county boundaries based on enlarged viewport area; normal panel zoom thresholds remain unchanged |
| `prefetchHours` | Forecast hours to consider for ahead-of-time downloading (24) |
| `prefetchDiskCacheMB` | Target compressed browser cache budget (512 MB) |
| `exportPanelWidth` / `exportAllWidth` | Output PNG widths for 16:9 exports (1920 / 2560 pixels) |

The available fields, runs, and hours are determined by the published model catalogs. Model data may legitimately be unavailable for some selections.

## Troubleshooting

- **A precipitation comparison is missing:** Verify the hourly MRMS QPE analysis catalog includes every valid hour from F001 through the selected F-hour. For model 1-hour values at F002 or later, both the current and previous cumulative forecast hours must exist. At F001, only the F001 accumulation is needed; F000 does not need to be published. Long accumulated MRMS periods can require substantial browser downloads.
- **A panel has no data:** Check the relevant `data/catalog_*.json`, its referenced Zarr paths, and the selected cycle/hour. An explicit `run=` URL parameter can pin an older cycle.
- **Map or GPU initialization fails:** Open `diagnostics.html`, then verify the MapLibre CSP worker, autumnplot-gl worker assets, and browser WebGL support.
- **Mobile maps load slowly:** Allow sequential initialization to finish. Reducing active maps in `config.json` may help on memory-constrained devices.
- **PNG geography is unexpectedly incomplete:** Confirm that the `data/map_*.geojson` files are accessible and inspect browser network errors. The exporter loads the geographic layers required at the current zoom before capturing.

**Rendering libraries:** [autumnplot-gl](https://github.com/tsupinie/autumnplot-gl) and MapLibre GL JS.

## Full Analysis rendering errors

If an Analysis panel reports a rendering error, tap or click its message.
The complete error, field, catalog dataset, grid type, array dimensions,
and Zarr store are shown in an alert and copied to the clipboard where
clipboard permissions allow it. This diagnostics enhancement does not change
meteorological data or map-grid interpretation.

**REFS mean sources:** CRAIN/CFRZR/CICEP/CSNOW, ASNOW and WEASD are from `mean` GRIB files; composite reflectivity is from the `pmmn` GRIB product. An unidentified local GRIB field is not presented as PTYPE until its meaning is verified.
