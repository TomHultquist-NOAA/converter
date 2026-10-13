#!/usr/bin/env bash
# Run from the SAME WCOSS2 interactive environment in which manual conversion works.
# Captures resolved executable paths and runtime library paths for cron, avoiding
# modulecmd and MODULEPATH bootstrap failures in non-login cron shells.
set -euo pipefail
# Some WCOSS2 wgrib2 builds attempt to launch more OpenMP threads than
# permitted on login nodes, even for `-version`. Keep the capture test
# identical to the one-thread setting already used by the converter.
export OMP_NUM_THREADS=1
export OMP_THREAD_LIMIT=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CONVERTER_RUNTIME_FILE:-$BASE_DIR/converter_runtime_paths.sh}"

PY="${PYTHON_BIN:-$(type -P python3 || true)}"
WG="${WGRIB2_BIN:-$(type -P wgrib2 || true)}"
if [[ -z "$PY" || "$PY" != /* || ! -x "$PY" ]]; then
  echo "ERROR: No absolute executable Python 3 path. Load your working modules first." >&2
  exit 2
fi
if [[ -z "$WG" || "$WG" != /* || ! -x "$WG" ]]; then
  echo "ERROR: No absolute executable wgrib2 path. Load your working modules first." >&2
  exit 2
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "ERROR: This runtime needs Python >=3.10. Found: $($PY --version 2>&1)" >&2
  exit 2
fi
echo "Testing wgrib2: $WG (OMP_NUM_THREADS=$OMP_NUM_THREADS)"
# On some WCOSS2 wgrib2 builds, `-version` prints a valid version number
# but returns a nonzero status. A strictly version-only response proves that
# the executable loaded and ran; extra diagnostic text is *not* accepted.
WG_TEST_STATUS=0
WG_TEST_OUTPUT="$("$WG" -version 2>&1)" || WG_TEST_STATUS=$?
if [[ "$WG_TEST_OUTPUT" =~ ^[[:space:]]*[vV]?([0-9]+\.)+[0-9]+[[:space:]]*$ ]]; then
  printf 'wgrib2 test passed: %s (version-query exit=%s)\n' "$WG_TEST_OUTPUT" "$WG_TEST_STATUS"
else
  echo "ERROR: wgrib2 did not return a clean version string." >&2
  echo "wgrib2 -version exit status: $WG_TEST_STATUS" >&2
  echo "Executable: $WG" >&2
  echo "OMP_NUM_THREADS=$OMP_NUM_THREADS; OMP_THREAD_LIMIT=$OMP_THREAD_LIMIT" >&2
  echo 'Actual wgrib2 output:' >&2
  printf '%s\n' "$WG_TEST_OUTPUT" >&2
  echo 'If this is a shared-library error, run capture_converter_runtime.sh from the exact interactive environment used for successful conversions.' >&2
  echo 'You may select the verified binary by running: WGRIB2_BIN=/absolute/path/to/wgrib2 ./capture_converter_runtime.sh' >&2
  exit 2
fi

# Capture shared-library and binary search paths from the proven-good session.
# Excludes secrets, SSH credentials, and unrelated shell functions/aliases.
# Never write a generated runtime file into an archive intended for others.
umask 077
TMP="${DEST}.tmp.$$"
trap 'rm -f "$TMP"' EXIT
{
  printf '#!/usr/bin/env bash\n'
  printf '# Generated locally by capture_converter_runtime.sh. Do not distribute.\n'
  printf '# Regenerate when the WCOSS2 software environment changes.\n'
  printf 'export PYTHON_BIN=%q\n' "$PY"
  printf 'export WGRIB2_BIN=%q\n' "$WG"
  printf 'export PATH=%q\n' "$PATH"
  printf 'export LD_LIBRARY_PATH=%q\n' "${LD_LIBRARY_PATH:-}"
  printf 'export CRAY_LD_LIBRARY_PATH=%q\n' "${CRAY_LD_LIBRARY_PATH:-}"
  printf 'export OMP_NUM_THREADS=1\n'
  printf 'export OMP_THREAD_LIMIT=1\n'
  printf 'export MKL_NUM_THREADS=1\n'
  printf 'export OPENBLAS_NUM_THREADS=1\n'
  printf 'export NUMEXPR_NUM_THREADS=1\n'
} > "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$DEST"
trap - EXIT
printf 'Saved tested runtime to %s\n' "$DEST"
printf 'Python: %s (%s)\n' "$PY" "$("$PY" --version 2>&1)"
printf 'wgrib2: %s\n' "$WG"
printf 'Test cron environment with:\n  env -i HOME="$HOME" PATH=/usr/bin:/bin CHECK_ENV_ONLY=1 /bin/bash %q\n' "$BASE_DIR/run_multimodel_cron.sh"
