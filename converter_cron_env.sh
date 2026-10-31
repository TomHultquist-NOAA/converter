#!/usr/bin/env bash
# Import an already tested WCOSS2 runtime for non-interactive cron.
# Run capture_converter_runtime.sh ONCE from your WORKING INTERACTIVE shell.
# Cron then requires neither an interactive .bashrc nor a functioning MODULEPATH.
# This file is sourced by run_multimodel_cron.sh.

_runtime_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
_runtime_file="${CONVERTER_RUNTIME_FILE:-${_runtime_dir}/converter_runtime_paths.sh}"
if [[ ! -r "$_runtime_file" ]]; then
  echo "Missing cron runtime file: $_runtime_file" >&2
  echo "From your working WCOSS2 login shell, run: ./capture_converter_runtime.sh" >&2
  return 1
fi
source "$_runtime_file"

if [[ -z "${PYTHON_BIN:-}" || ! -x "$PYTHON_BIN" ]]; then
  echo "PYTHON_BIN is missing or not executable: ${PYTHON_BIN:-<unset>}" >&2
  return 1
fi
if [[ -z "${WGRIB2_BIN:-}" || ! -x "$WGRIB2_BIN" ]]; then
  echo "WGRIB2_BIN is missing or not executable: ${WGRIB2_BIN:-<unset>}" >&2
  return 1
fi

# Avoid accidentally importing an unrelated Python user site / environment.
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONHOME PYTHONPATH
unset _runtime_file _runtime_dir
