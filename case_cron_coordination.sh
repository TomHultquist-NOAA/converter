#!/usr/bin/env bash
# Shared with both cron wrappers; case creation takes exclusive locks in Python.
# Called after cron runtime/configuration checks but before its own writer lock.
#   fd 6: brief shared startup admission gate
#   fd 7: shared execution gate, held throughout this cron invocation
# The case holds both exclusively while processing, preventing new cron work.
# No process-name matching, timeouts, lock-file deletion, or cron edits required.
acquire_cron_case_gate() {
  local directory="$1"
  CRON_SKIP_REASON=""
  exec 6>>"$directory/.case_startup.lock"
  if ! flock -n -s 6; then
    CRON_SKIP_REASON=case_startup_priority_lock_busy
    echo 'Cron skipped: historical case is waiting or running; next scheduled invocation will retry.'
    exec 6>&-
    return 1
  fi
  exec 7>>"$directory/.case_cron_gate.lock"
  if ! flock -n -s 7; then
    CRON_SKIP_REASON=case_execution_gate_busy
    echo 'Cron skipped: historical case holds the processing gate; next scheduled invocation will retry.'
    exec 7>&-
    exec 6>&-
    return 1
  fi
  # Keep the shared execution gate (fd 7) until the cron shell exits.
  # Release the short-lived admission lock so a case can announce priority.
  flock -u 6
  exec 6>&-
  return 0
}
