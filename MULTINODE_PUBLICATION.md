# Concurrent cron publication and cross-node locking

The model, verification, and GFS cron wrappers retain their existing shared
filesystem `flock` and case-priority gates. Cross-host enforcement depends on
the mounted filesystem and cannot be guaranteed from a code-only inspection.
Do not remove or recreate active `.lock` files, because doing so can bypass
protection for already-running processes.

## Verify cross-node `flock` before relying on shared cron gates

On one login node, execute (using the configured Python 3 interpreter):

```sh
python3 check_shared_locks.py hold /path/to/converter --seconds 90
```

While that command is holding the lock, execute on **a different login node**
sharing the same converter directory:

```sh
python3 check_shared_locks.py probe /path/to/converter
```

The second command must print `BUSY` and exit 0. If it prints `UNSAFE`,
filesystem locks are not working across those hosts; do not run competing cron
writers or historical case processing until an administrator can confirm an
appropriate locking mechanism. This diagnostic touches only its own test lock.

## Sounding-profile catalog publication

The GFS and regular model workflows both publish `catalog_profiles.json`.
Each operation now uses a unique hidden staging filename on the remote host,
and installation and the GFS/regular remote retention sweeps hold a remote-host `flock` while merging the individual
store entries. Independent catalogs cannot overwrite another model group's
entries. Explicitly expired profiles are removed from the catalog before
store deletion. The destination must support `flock` and Python 3; failure
is reported instead of silently publishing without a lock.

The existing SSH and SCP transport metrics continue to record each operation.
An SSH exit code of 1 is a remote execution failure, not proof of a network
outage. Login banners and the warning that a ControlSocket exists can appear
without causing a failure. For troubleshooting, inspect the full messages
immediately before the traceback.

Remote staging files left behind by interrupted operations are intentionally
not deleted during an active publication. Their distinct filenames prevent
collisions; older leftover staging artifacts can be cleaned in a separate,
conservative maintenance sweep after verifying no matching writer is active.


### Hourly RAP profile catalog compatibility (2026-11-03)
The shared sounding catalog publisher accepts RAP analysis profile stores at every UTC hour (00–23). Forecast model sounding stores retain their existing synoptic-hour validation. The same rules apply to remote catalog merge and expired-store cleanup; this corrects false publication failures for valid hourly RAP profiles without relaxing path restrictions.
