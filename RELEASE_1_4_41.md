# EVU Converter v1.4.41 — November 9, 2026

- Extend the level-restricted native specific-humidity and dewpoint map fields to 925 hPa for GFS v16/v17, AIGFS, AIGEFS, HGEFS, GEFS and available regional model sources. Preserve the 700/850-hPa paths and no-other-level pressure filtering.
- Dewpoint may be computed at the same isobaric pressure from native specific humidity and temperature when GRIB dewpoint is absent. Missing fields remain absent rather than synthesized from other pressure levels.
- On-demand AI model cross-resolution differences are aligned geographically within the viewer rather than altering published native source stores; the existing GFS precomputed differences and RRFS analysis alignment remain unchanged.
- Release archive excludes developer tests and packaging internals, preserving production conversion, cron, maintenance, and data-transfer scripts.
