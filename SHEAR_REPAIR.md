# New RRFS/REFS 0–1 km shear repair (v1.4.8)

**Do not use the legacy `repair_bulk_shear.py` multiplier-based repair to fix RRFS/REFS 0–1 km shear.** The native fields in the investigated source product are suspect, so the converter now derives the 0–1 km wind difference from native pressure-level winds, heights, surface pressure, and 10 m winds. For the 2026102812 case, use:

```bash
python3 create_case.py --cycle 2026102812 --lowlevel-shear-only --execute
```

This command preserves other forecast fields, analysis, profiles and observations. See the current release notes. The historical instructions below apply only to earlier, distinct unit-scaling problems.

---

# Bulk-shear recovery for existing Zarr cycles

Legacy converter versions could multiply `VUCSH` / `VVCSH` components that were already wind differences in m/s by 1000 or 6000 m. This produced unrealistic knots in the viewer. Converter v1.2.2 uses a source-aware unit check before deriving future fields. Previously published scalar `shear01` and `shear06` chunks require repair.

Do not reprocess or replace complete Zarr cycles merely to correct these derived scalars. From the processing server `zarr_processing` directory, pause the model watcher and hourly processing during the repair. First preview:

```bash
python3 repair_bulk_shear.py --date 20261029 --cycle 00 --models all \
  --output-root "$PWD/zarr"
```

Then execute:

```bash
python3 repair_bulk_shear.py --date 20261029 --cycle 00 --models all \
  --output-root "$PWD/zarr" --execute
```

Publish the corrected cycle to web server:

```bash
python3 multi_model_to_zarr_wcoss2.py --date 20261029 --cycle 00 \
  --models all --publish-only --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --protected-cycles-file "$PWD/protected_cycles.json" \
  --no-local-cleanup --no-remote-cleanup
```

The repair script updates only `shear01` and `shear06` Zarr chunks and their derived-field provenance when the native shear components are present. If the components are absent, an appropriate missing-field repair is needed first. It does not modify the original component chunks or unrelated fields. Review the numerical values and run summary after publication.

## Native RAP analysis correction (v1.3.7)

The forecast-model repair utility above does **not** repair RAP native analysis.
RAP `VUCSH` / `VVCSH` can contain either native gradients (s^-1) or wind
component differences (m/s). Earlier RAP conversion always multiplied by
6000 m; this could create extreme scalar magnitudes and discarded the U/V
components needed for overlay arrows. The current converter checks the paired
component magnitudes, retains correctly normalized U/V, masks physically
implausible values, and refuses ambiguous units. It also extracts native
Bunkers right-moving `USTM` / `VSTM` components for SRH barbs.

Replacing the code does not rewrite already-published native analysis. During a
controlled maintenance window, pause the regular verification job and restore
specific case valid hours using the shared converter entry point (replace the
example valid-time range and two directory paths):

```bash
python3 multi_model_to_zarr_wcoss2.py --verification-only \
  --source rap --start-hour 2026102812 --end-hour 2026102918 \
  --output-root "$PWD/zarr" --work-root "$PWD/work" \
  --protected-cycles-file "$PWD/protected_cycles.json" \
  --publish-workers 3 --no-local-cleanup
```

The verification pipeline detects stale source inventories and older RAP
conversion metadata, reacquires the necessary GRIB records when staged input
is unavailable, reconstructs the RAP fields, and republishes the native
analysis catalog only after successful transfers. The selected interval must
be no longer than 168 hours. Protected valid hours remain protected. For an
older case outside normal retention, ensure the protected-case configuration
covers the range **before** processing. Examine the log for `RAP 0-6000m shear
components` and check the `shear06` range in the viewer afterward. This
procedure does not overwrite forecast-model stores.
