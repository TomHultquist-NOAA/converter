#!/usr/bin/env bash
# Show the latest scheduled wrapper invocations and their recorded outcomes.
set -euo pipefail
BASE_DIR="${CRON_BASE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
LOG_DIR="${CRON_LOG_DIR:-$BASE_DIR/logs}"

show_recent() {
  local label="$1" pattern="$2" file found=0
  printf '\n%s\n' "$label"
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    found=1
    printf '  %s\n' "$(basename "$file")"
    if ! grep -E '^=== INVOCATION (START|END)' "$file" | tail -n 2 | sed 's/^/    /'; then
      :
    fi
    if ! grep -q '^=== INVOCATION END' "$file"; then
      printf '    No completion marker: possibly still running or terminated unexpectedly.\n'
    fi
  done < <(ls -1t "$LOG_DIR"/$pattern 2>/dev/null | head -n 5 || true)
  if (( found == 0 )); then
    printf '  No invocation logs found.\n'
  fi
}

# The GFS poller uses POLL START/END markers (rather than INVOCATION).
# SSH login banners can be appended to a marker without a newline, so markers
# must be searched *within* each line, not only at the beginning.
show_recent_gfs() {
  local file found=0 start end skipped status pid
  printf '\n%s\n' '30-minute GFS incremental cron:'
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    found=1
    printf '  %s\n' "$(basename "$file")"
    start="$(grep -oE 'GFS POLL START [^[:cntrl:]]*' "$file" | tail -n 1 || true)"
    end="$(grep -oE 'GFS POLL END [^[:cntrl:]]*' "$file" | tail -n 1 || true)"
    skipped="$(grep -oE 'GFS poll skipped: [^[:cntrl:]]*' "$file" | tail -n 1 || true)"
    [[ -z "$start" ]] || printf '    %s\n' "$start"
    if [[ -n "$end" ]]; then
      printf '    %s\n' "$end"
      if [[ "$end" =~ status=([0-9]+) ]]; then
        status="${BASH_REMATCH[1]}"
        if [[ "$status" == 0 ]]; then
          printf '    Result: SUCCESS\n'
        else
          printf '    Result: FAILED (exit status %s)\n' "$status"
        fi
      else
        printf '    Result: ENDED (exit status not recorded)\n'
      fi
    elif [[ -n "$skipped" ]]; then
      printf '    %s\n    Result: SKIPPED\n' "$skipped"
    elif grep -q 'GFS workflow disabled' "$file"; then
      printf '    Result: DISABLED\n'
    else
      pid=""
      if [[ "$start" =~ pid=([0-9]+) ]]; then pid="${BASH_REMATCH[1]}"; fi
      # A local PID check is suggestive only: a poll may have run on another
      # host, or the PID may have been reused by an unrelated process.
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        printf '    Result: NO END MARKER (PID %s exists locally; verify process)\n' "$pid"
      else
        printf '    Result: INCOMPLETE (no end/skip marker; process may have exited)\n'
      fi
    fi
  done < <(ls -1t "$LOG_DIR"/gfs_incremental_*.log 2>/dev/null | head -n 5 || true)
  if (( found == 0 )); then
    printf '  No GFS poll logs found.\n'
  fi
}

printf 'OMD EVU cron status\nLog directory: %s\n' "$LOG_DIR"
show_recent 'Hourly verification / observations cron:' 'multisource_*.log'
show_recent '15-minute model / sounding cron:' 'model_incremental_*.log'
show_recent_gfs
printf '\nA START without END may indicate a running, skipped, or interrupted invocation.\n'
printf 'If no new START appears at an expected time, check the installed crontab and the host cron-service logs.\n'
