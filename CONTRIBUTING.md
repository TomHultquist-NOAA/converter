# Converter contribution and release safeguards

- Run tests with the same Python minor version used in the installation:
  `python3 -m unittest discover -s tests -p 'test_*.py' -q`.
- Review source availability and actual GRIB2 inventory before changing field
  selection, timestamps, units or interpolation.
- Add a regression test for each modification to CCPA/Stage IV/MRMS source
  precedence, verified accumulation completeness and model differences.
- Test `--mode missing`, `--force-hours`, protected-cycle recovery, and
  startup/cron locks when changing job selection.
- Never put credentials or the live protected-cycle file in GitHub.
- Preserve backwards compatibility with deployed catalog formats, user
  preferences and the existing viewer's soundings/analysis expectations.
- Do not silently cap worker settings. Retain operational locking and
  document memory/SSH risks where relevant.

## Reproducible packaging

`python3 tools/package_converter.py /tmp/EVU_Converter_release.zip` creates a
source-only archive with `755` executable scripts and `644` documentation.
The builder verifies the recorded file manifest and refuses to include
operational credentials, protected-cycle state, logs or forecast stores.
