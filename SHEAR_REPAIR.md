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
