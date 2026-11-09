## v1.4.38 upgrade — read before deploying

Follow AI_MODEL_SET_INSTALL.md. Keep operational config, protected cases and all model/analysis data. Activate the new AI cron only after testing --plan, --inventory-only, and the local first cycle. The existing verification and GFS/RRFS cron entries remain unchanged.

# Converter installation / upgrade — Linux

The processing environment needs its **existing** Python runtime and
`wgrib2`; this package does not install them. No new `pip` packages are
required by this release.

1. Back up the currently working converter directory and preserve site-specific
   `converter_config.sh`, runtime-path scripts, secret files, cron configuration,
   publication keys, `protected_cycles.json` and forecast/history directories.
2. Extract `EVU_Converter_v1.4.35_docs_audit.zip` in your existing converter
   directory. Do not replace your protected-cycle JSON with the example file.
3. Confirm executable scripts retain mode 755 and static documentation
   mode 644. A packaged release inventory is in `packaged_static_files.txt`.
4. Validate syntax and tests where Node isn't required:

   ```sh
   python3 -m compileall -q .
   python3 -m unittest discover -s tests -p 'test_*.py' -q
   ./check_cron_status.sh
   ```

5. Before any actual processing, inspect `converter_config.sh`, `--help`
   and the protected-cycle state. Use existing dry-run/inventory-only paths
   for historical cases and retention before production runs.

Rollback: restore the previous converter ZIP/working snapshot, leaving all
published and protected Zarr stores, credentials, logs, and runtime paths
unchanged. This version is documentation-only relative to v1.4.34.

For GitHub/source packaging, run
`python3 tools/package_converter.py /tmp/EVU_Converter_release.zip`.
The release manifest must match exactly and no operational state is packaged.
