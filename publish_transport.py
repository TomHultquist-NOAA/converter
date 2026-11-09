#!/usr/bin/env python3
"""Bounded, stdlib-only SSH/SCP connection sharing for Zarr publication.

The caller owns transaction ordering; this helper only reuses authenticated SSH
connections for independent transfers. No persistent credentials, agent, or
network access beyond normal system SSH/SCP is introduced. The temporary
private control socket is removed at session exit; SSH will fall back to a
regular connection if the server refuses multiplexing.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
import subprocess
import tempfile
import time
import os
import perf_monitor

_SESSION_ENV = 'EVU_SSH_CONTROL_PATH'

@contextmanager
def session():
    if os.environ.get(_SESSION_ENV):
        yield
        return
    with tempfile.TemporaryDirectory(prefix='evu-ssh-') as directory:
        os.chmod(directory,0o700)
        previous=os.environ.get(_SESSION_ENV)
        os.environ[_SESSION_ENV]=os.path.join(directory,'control')
        try:yield
        finally:
            if previous is None:os.environ.pop(_SESSION_ENV,None)
            else:os.environ[_SESSION_ENV]=previous

def command_args(args):
    args=list(args)
    if args and args[0] in ('ssh','scp') and os.environ.get(_SESSION_ENV):
        # ControlMaster=auto is opportunistic, and does not require prestarting
        # an SSH daemon. Keep the same socket across the publication workers.
        option=['-o','ControlMaster=auto','-o','ControlPersist=90',
                '-o','ControlPath='+os.environ[_SESSION_ENV],
                '-o','BatchMode=yes','-o','ConnectTimeout=20']
        return [args[0],*option,*args[1:]]
    return args

def run(args, *, retries=3, **kwargs):
    """Retry transient SSH transport failures only; never mask remote errors.

    Catalog installation is idempotent, and caller-supplied .incoming paths are
    unique by store. Exponential backoff is deliberately short and bounded.
    """
    args=list(args)
    if args[0] not in ('ssh','scp'):raise ValueError('Transport supports ssh/scp only')
    retries=max(1,min(5,int(retries)))
    shared=True
    for attempt in range(retries):
        started = None
        transfer_id = None
        try:
            if shared:
                cmd=command_args(args)
            else:
                # A previous timeout may indicate a stalled shared master.
                # Retry independently rather than reusing that same socket.
                cmd=[args[0],'-o','BatchMode=yes','-o','ConnectTimeout=20',*args[1:]]
            started = time.monotonic()
            transfer_id = '%s-%s-%s' % (os.getpid(), time.time(), attempt + 1)
            perf_monitor.record_event('transfer_start', operation=args[0], transfer_id=transfer_id)
            result = subprocess.run(cmd,**kwargs)
            elapsed = time.monotonic() - started
            size = 0
            if args[0] == 'scp' and len(args) >= 3:
                candidate = args[-2]
                try:
                    if os.path.isfile(candidate): size = os.path.getsize(candidate)
                except (OSError, TypeError):
                    pass
            perf_monitor.record_event('transfer', operation=args[0], transfer_id=transfer_id, bytes=size,
                elapsed_seconds=round(elapsed, 4), attempt=attempt + 1,
                status='complete' if getattr(result, 'returncode', 0) == 0 else 'failed')
            perf_monitor.add_metric('ssh_scp_command_seconds', elapsed)
            perf_monitor.add_metric('ssh_scp_local_bytes', size)
            return result
        except subprocess.CalledProcessError as exc:
            elapsed = time.monotonic() - started if started is not None else 0.0
            perf_monitor.record_event('transfer', operation=args[0],
                transfer_id=locals().get('transfer_id'), bytes=0,
                elapsed_seconds=round(elapsed, 4), attempt=attempt + 1,
                status='failed', returncode=exc.returncode)
            perf_monitor.add_metric('ssh_scp_command_seconds', elapsed)
            if exc.returncode!=255 or attempt==retries-1:raise
        except OSError as exc:
            elapsed = time.monotonic() - started if started is not None else 0.0
            perf_monitor.record_event('transfer', operation=args[0],
                transfer_id=transfer_id, bytes=0,
                elapsed_seconds=round(elapsed, 4), attempt=attempt + 1,
                status='os_error', error_type=type(exc).__name__)
            perf_monitor.add_metric('ssh_scp_command_seconds', elapsed)
            raise
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started if started is not None else 0.0
            perf_monitor.record_event('transfer', operation=args[0],
                transfer_id=locals().get('transfer_id'), bytes=0,
                elapsed_seconds=round(elapsed, 4), attempt=attempt + 1, status='timeout')
            perf_monitor.add_metric('ssh_scp_command_seconds', elapsed)
            shared=False
            if attempt==retries-1:raise
        except (TimeoutError,ConnectionError):
            elapsed = time.monotonic() - started if started is not None else 0.0
            perf_monitor.record_event('transfer', operation=args[0],
                transfer_id=locals().get('transfer_id'), bytes=0,
                elapsed_seconds=round(elapsed, 4), attempt=attempt + 1, status='connection_error')
            perf_monitor.add_metric('ssh_scp_command_seconds', elapsed)
            shared=False
            if attempt==retries-1:raise
        time.sleep(min(2**attempt,4))
