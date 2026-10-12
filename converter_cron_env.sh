#!/bin/bash
# WCOSS2 modules required by the converter when launched from cron.
# Sourced by run_multimodel_cron.sh; this is NOT a login shell.

# "module" is often a shell function absent from cron. Initialize it without
# loading full system/user login profiles. Set MODULE_INIT_FILE below if your
# WCOSS2 module setup script lives somewhere else.
MODULE_INIT_FILE="${MODULE_INIT_FILE:-}"
if ! type module >/dev/null 2>&1; then
  if [[ -n "$MODULE_INIT_FILE" && -r "$MODULE_INIT_FILE" ]]; then
    source "$MODULE_INIT_FILE"
  else
    found_init=0
    for candidate in \
      "${MODULESHOME:-/nonexistent}/init/bash" \
      /etc/profile.d/lmod.sh \
      /etc/profile.d/modules.sh \
      /usr/share/Modules/init/bash; do
      if [[ -r "$candidate" ]]; then
        source "$candidate"
        found_init=1
        break
      fi
    done
    if [[ "$found_init" -eq 0 ]]; then
      echo "Cannot find modules initialization: set MODULE_INIT_FILE to the site-specific initialization script" >&2
      return 1
    fi
  fi
fi
if ! type module >/dev/null 2>&1; then
  echo 'The module command is unavailable after initialization.' >&2
  return 1
fi

# Based on the user's working .bashrc, but restricted to the compiler/runtime,
# wgrib2 and Python toolchain. The converter uses only the Python stdlib.
module load intel/19.1.3.304
module load PrgEnv-intel/8.3.3
module load craype/2.7.17
module load cray-mpich/8.1.19
module load wgrib2/3.8.0
module load python/3.10.4

# Do not inherit Python overrides from ~/.bashrc's unrelated NBM virtual env.
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONHOME PYTHONPATH

# To use the NBM virtual environment instead, uncomment these ONLY if required:
# export PATH="/apps/prod/ve/intel/19.1.3.304/python/3.10.4/nbm/5.0/bin:$PATH"
# export LD_LIBRARY_PATH="/apps/prod/ve/intel/19.1.3.304/python/3.10.4/nbm/5.0/lib:${LD_LIBRARY_PATH:-}"
# export VIRTUAL_ENV="/apps/prod/ve/intel/19.1.3.304/python/3.10.4/nbm/5.0"
