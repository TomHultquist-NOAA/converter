# Operational RRFS/REFS polling adjustment — November 3, 2026

The existing regular-model incremental converter already uses the operational RRFS/REFS public AWS source. Following the operational implementation, its default latest-cycle selection lag is shortened from 0.75 h (45 min) to 0.25 h (15 min).

**Required for existing configurations:** In `converter_config.sh`, change `export INCREMENTAL_CYCLE_LAG_HOURS=0.75` to `export INCREMENTAL_CYCLE_LAG_HOURS=0.25`. User configuration takes precedence over script defaults. No other changes are required.

This change only starts availability checks earlier; it does not guarantee GRIBs are available at 15 minutes. Missing data remain eligible for retry, and the existing catchup/worker/retention/lock behavior is unchanged. No RRFS/REFS paths or viewer files were modified.

Do not overwrite converter scripts while an active cron or historical case is running.
