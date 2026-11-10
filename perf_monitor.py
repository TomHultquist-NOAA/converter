#!/usr/bin/env python3
"""Low-overhead, standard-library performance monitoring for OMD EVU Converter.

The monitor samples a process tree through Linux /proc and writes separate
JSONL samples/events plus concise JSON/text summaries. It deliberately avoids
psutil and does not alter worker limits or processing behavior.
"""
from __future__ import print_function

import argparse
from contextlib import contextmanager
import datetime
import functools
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback

# Per-thread counters prevent overlapping hourly worker tasks from crediting
# another worker's compression/download bytes to their own stage. We keep
# process-wide resource sampling in PerformanceMonitor unchanged.
_METRICS_LOCAL = threading.local()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _safe_number(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _write_json_line(path, payload):
    """Append one compact JSON record; monitoring failures never fail science."""
    if not path:
        return
    try:
        encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except Exception:
        # Performance telemetry is strictly best-effort and must not interrupt
        # forecast, analysis, sounding, or publication processing.
        pass


def record_event(event_type, **values):
    path = os.environ.get('EVU_PERF_EVENTS_FILE')
    if not path:
        return
    payload = {'event_type': event_type, 'utc': _utc_now(), 'epoch': time.time(), 'pid': os.getpid()}
    payload.update(values)
    _write_json_line(path, payload)


def add_metric(name, value):
    """Add a process-local counter/timer, later attached to a task event."""
    try:
        value = float(value)
        if not math.isfinite(value):
            return
        bucket = getattr(_METRICS_LOCAL, 'values', None)
        if bucket is None:
            bucket = {}
            _METRICS_LOCAL.values = bucket
        bucket[name] = bucket.get(name, 0.0) + value
    except Exception:
        pass


def metric_snapshot():
    return dict(getattr(_METRICS_LOCAL, 'values', {}))


def metric_delta(before):
    after = metric_snapshot()
    return {key: after.get(key, 0.0) - before.get(key, 0.0)
            for key in set(before) | set(after)
            if abs(after.get(key, 0.0) - before.get(key, 0.0)) > 0}


@contextmanager
def stage(name, **details):
    """Record an elapsed-time event for a named processing stage."""
    started = time.monotonic()
    record_event('stage_start', stage=name, details=details)
    status = 'complete'
    error = None
    try:
        yield
    except BaseException as exc:
        status = 'failed'
        error = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        record_event('stage_end', stage=name,
                     elapsed_seconds=round(time.monotonic() - started, 4),
                     status=status, error=error, details=details)


def timed_task(category, labeler=None, include_metrics=True):
    """Decorator for per-hour/per-store worker functions.

    Set include_metrics=False for sub-stages nested inside hourly tasks, so
    their timing is reported without double-counting the same compression,
    download or disk-write counters in aggregate metrics.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            # A disabled opt-in monitor must not incur per-task instrumentation
            # overhead on the ordinary forecast processing path.
            if not os.environ.get('EVU_PERF_EVENTS_FILE'):
                return function(*args, **kwargs)
            started = time.monotonic()
            before = metric_snapshot() if include_metrics else {}
            label = None
            task_id = '%s-%s-%.6f' % (os.getpid(), threading.current_thread().ident, started)
            record_event('task_start', category=category, task_id=task_id)
            try:
                if labeler is not None:
                    try:
                        label = labeler(args, kwargs)
                    except Exception:
                        label = None
                result = function(*args, **kwargs)
                status = result.get('status', 'complete') if isinstance(result, dict) else 'complete'
                return result
            except BaseException:
                status = 'failed'
                raise
            finally:
                record_event('task', category=category, label=label, task_id=task_id,
                             elapsed_seconds=round(time.monotonic() - started, 4),
                             status=status,
                             metrics=metric_delta(before) if include_metrics else {})
        return wrapped
    return decorate


def monitored(label):
    """Decorate a script entry point with a process-tree monitor.

    Nested tools inherit EVU_PERF_ACTIVE and contribute task/stage events to
    the parent run instead of creating redundant overlapping monitors.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            if (os.environ.get('EVU_PERF_ENABLED', '0') != '1' or
                    os.environ.get('EVU_PERF_ACTIVE') == '1'):
                return function(*args, **kwargs)
            log_dir = os.environ.get('EVU_PERF_LOG_DIR')
            if not log_dir:
                raw_args = list(args[0]) if args and isinstance(args[0], (list, tuple)) else sys.argv[1:]
                work_root = output_root = root_arg = None
                for index, item in enumerate(raw_args):
                    if item == '--work-root' and index + 1 < len(raw_args):
                        work_root = raw_args[index + 1]
                    elif item.startswith('--work-root='):
                        work_root = item.split('=', 1)[1]
                    elif item == '--output-root' and index + 1 < len(raw_args):
                        output_root = raw_args[index + 1]
                    elif item.startswith('--output-root='):
                        output_root = item.split('=', 1)[1]
                    elif item == '--root' and index + 1 < len(raw_args):
                        root_arg = raw_args[index + 1]
                    elif item.startswith('--root='):
                        root_arg = item.split('=', 1)[1]
                if work_root:
                    base = Path(work_root)
                elif output_root:
                    base = Path(output_root).expanduser().parent
                elif root_arg:
                    base = Path(root_arg).expanduser().parent
                else:
                    base = Path(__file__).resolve().parent
                log_dir = str(base / 'logs' / 'performance')
            interval = _safe_number(os.environ.get('EVU_PERF_SAMPLE_SECONDS', '5'), 5.0)
            retention = _safe_number(os.environ.get('EVU_PERF_RETENTION_DAYS', '7'), 7.0)
            with PerformanceMonitor(label, log_dir, interval, retention) as monitor:
                monitor.set_command(function.__module__ + '.' + function.__name__)
                result = function(*args, **kwargs)
                if isinstance(result, int):
                    monitor.set_exit_status(result)
                return result
        return wrapped
    return decorate


def _read_proc(pid, read_io=True):
    base = '/proc/' + str(pid)
    try:
        with open(base + '/stat', 'r') as stream:
            raw = stream.read()
        close = raw.rfind(')')
        fields = raw[close + 2:].split()
        if len(fields) < 22:
            return None
        ppid = int(fields[1])
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
        rss_pages = max(0, int(fields[21]))
        io_read = io_write = 0
        if read_io:
            try:
                with open(base + '/io', 'r') as stream:
                    for line in stream:
                        if line.startswith('read_bytes:'):
                            io_read = int(line.split(':', 1)[1].strip())
                        elif line.startswith('write_bytes:'):
                            io_write = int(line.split(':', 1)[1].strip())
            except (IOError, OSError, ValueError):
                pass
        return {'pid': int(pid), 'ppid': ppid, 'starttime': starttime,
                'ticks': utime + stime, 'rss_bytes': rss_pages * _PAGE_SIZE,
                'read_bytes': io_read, 'write_bytes': io_write}
    except (IOError, OSError, ValueError, IndexError):
        return None


def _all_processes():
    found = {}
    try:
        names = os.listdir('/proc')
    except OSError:
        return found
    for name in names:
        if name.isdigit():
            item = _read_proc(int(name), read_io=False)
            if item is not None:
                found[item['pid']] = item
    return found


def _tree_processes(root_pid, root_starttime=None, exclude_pids=()):
    processes = _all_processes()
    root = processes.get(int(root_pid))
    if root is None or (root_starttime is not None and root['starttime'] != root_starttime):
        return {}
    children = {}
    for pid, item in processes.items():
        children.setdefault(item['ppid'], []).append(pid)
    excluded = set(int(x) for x in exclude_pids)
    selected = {}
    pending = [int(root_pid)]
    while pending:
        pid = pending.pop()
        if pid in excluded or pid in selected:
            continue
        item = processes.get(pid)
        if item is None:
            continue
        selected[pid] = item
        pending.extend(children.get(pid, ()))
    # Read potentially slower per-process I/O counters only for this run's tree,
    # not for every unrelated process on the shared host.
    for pid, item in selected.items():
        current = _read_proc(pid, read_io=True)
        if current is not None and current['starttime'] == item['starttime']:
            item['read_bytes'] = current['read_bytes']
            item['write_bytes'] = current['write_bytes']
    return selected


def _host_memory():
    total = available = None
    try:
        with open('/proc/meminfo', 'r') as stream:
            for line in stream:
                if line.startswith('MemTotal:'):
                    total = int(line.split()[1]) * 1024
                elif line.startswith('MemAvailable:'):
                    available = int(line.split()[1]) * 1024
    except (IOError, OSError, ValueError):
        pass
    return total, available


def _parse_cpuset(value):
    count = 0
    try:
        for part in value.strip().split(','):
            if not part:
                continue
            if '-' in part:
                left, right = part.split('-', 1)
                count += max(0, int(right) - int(left) + 1)
            else:
                count += 1
        return count or None
    except (TypeError, ValueError):
        return None


def _cgroup_limits(host_mem_total=None):
    """Best-effort cgroup v1/v2 quota/limit context, if exposed by the host."""
    result = {'cpu_quota_cores': None, 'cpuset_cpu_count': None,
              'memory_limit_bytes': None, 'memory_current_bytes': None}
    unified = None
    controllers = {}
    try:
        with open('/proc/self/cgroup', 'r') as stream:
            for line in stream:
                parts = line.strip().split(':', 2)
                if len(parts) != 3:
                    continue
                if parts[0] == '0':
                    unified = parts[2].lstrip('/')
                for name in parts[1].split(','):
                    if name:
                        controllers[name] = parts[2].lstrip('/')
    except (IOError, OSError):
        pass

    def read_value(path):
        try:
            with open(str(path), 'r') as stream:
                return stream.read().strip()
        except (IOError, OSError):
            return None

    if unified is not None:
        base = Path('/sys/fs/cgroup') / unified
        cpu = read_value(base / 'cpu.max')
        if cpu:
            fields = cpu.split()
            if len(fields) >= 2 and fields[0] != 'max':
                try:
                    quota, period = float(fields[0]), float(fields[1])
                    if quota > 0 and period > 0:
                        result['cpu_quota_cores'] = quota / period
                except ValueError:
                    pass
        mem_limit = read_value(base / 'memory.max')
        mem_current = read_value(base / 'memory.current')
        cpuset = read_value(base / 'cpuset.cpus.effective') or read_value(base / 'cpuset.cpus')
    else:
        memory_path = controllers.get('memory')
        cpu_path = controllers.get('cpu')
        cpuset_path = controllers.get('cpuset')
        memory_base = Path('/sys/fs/cgroup/memory') / memory_path if memory_path is not None else None
        cpu_base = Path('/sys/fs/cgroup/cpu') / cpu_path if cpu_path is not None else None
        cpuset_base = Path('/sys/fs/cgroup/cpuset') / cpuset_path if cpuset_path is not None else None
        mem_limit = read_value(memory_base / 'memory.limit_in_bytes') if memory_base else None
        mem_current = read_value(memory_base / 'memory.usage_in_bytes') if memory_base else None
        quota = read_value(cpu_base / 'cpu.cfs_quota_us') if cpu_base else None
        period = read_value(cpu_base / 'cpu.cfs_period_us') if cpu_base else None
        if quota and period:
            try:
                if int(quota) > 0 and int(period) > 0:
                    result['cpu_quota_cores'] = float(quota) / float(period)
            except ValueError:
                pass
        cpuset = (read_value(cpuset_base / 'cpuset.effective_cpus') or
                  read_value(cpuset_base / 'cpuset.cpus')) if cpuset_base else None

    if cpuset:
        result['cpuset_cpu_count'] = _parse_cpuset(cpuset)
    try:
        if mem_limit and mem_limit != 'max':
            limit = int(mem_limit)
            # cgroup v1 often represents unlimited as a huge sentinel.
            if limit > 0 and (host_mem_total is None or limit < host_mem_total * 2):
                result['memory_limit_bytes'] = limit
        if mem_current and mem_current != 'max':
            result['memory_current_bytes'] = int(mem_current)
    except ValueError:
        pass
    return result


_PAGE_SIZE = int(os.sysconf('SC_PAGE_SIZE')) if hasattr(os, 'sysconf') else 4096
_TICKS_PER_SECOND = int(os.sysconf('SC_CLK_TCK')) if hasattr(os, 'sysconf') else 100


class PerformanceMonitor(object):
    def __init__(self, label, log_dir, interval=5.0, retention_days=7,
                 root_pid=None, exclude_pids=()):
        self.label = ''.join(c if c.isalnum() or c in '._-' else '_' for c in str(label))[:48]
        self.log_dir = Path(log_dir).expanduser()
        self.interval = max(1.0, min(60.0, float(interval or 5.0)))
        self.retention_days = max(1, min(366, int(retention_days or 7)))
        self.root_pid = int(root_pid if root_pid is not None else os.getpid())
        self.exclude_pids = set(int(x) for x in exclude_pids)
        self.root_starttime = None
        self.run_id = None
        self.samples_path = None
        self.events_path = None
        self.summary_path = None
        self.text_path = None
        self.started = None
        self._previous = None
        self._previous_time = None
        self._samples = []
        self._cpu_seconds_cumulative = 0.0
        self._io_read_cumulative = 0
        self._io_write_cumulative = 0
        self._stop = threading.Event()
        self._thread = None
        self._old_env = {}
        self._enabled = False
        self.command = None
        self._exit_status = None

    def set_command(self, value):
        self.command = value

    def set_exit_status(self, value):
        try:
            self._exit_status = int(value)
        except (TypeError, ValueError):
            self._exit_status = None

    def _prepare_paths(self):
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        try:
            self.log_dir.chmod(0o750)
        except OSError:
            pass
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        self.run_id = 'evu_perf_' + stamp + '_' + self.label + '_' + str(self.root_pid)
        self.samples_path = self.log_dir / (self.run_id + '_samples.jsonl')
        self.events_path = self.log_dir / (self.run_id + '_events.jsonl')
        self.summary_path = self.log_dir / (self.run_id + '_summary.json')
        self.text_path = self.log_dir / (self.run_id + '_summary.txt')
        for path in (self.samples_path, self.events_path):
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            os.close(fd)
        self._prune_old()

    def _prune_old(self):
        cutoff = time.time() - self.retention_days * 86400
        try:
            for path in self.log_dir.iterdir():
                if (path.name.startswith('evu_perf_') and
                        path.name.endswith(('_samples.jsonl', '_events.jsonl', '_summary.json', '_summary.txt')) and
                        not path.is_symlink() and path.is_file()):
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def __enter__(self):
        if os.environ.get('EVU_PERF_ENABLED', '0') != '1':
            return self
        self._enabled = True
        try:
            self._prepare_paths()
        except Exception:
            # Telemetry must never make a valid converter job fail because a
            # log filesystem is unavailable or read-only.
            self._enabled = False
            return self
        root = _read_proc(self.root_pid)
        self.root_starttime = root['starttime'] if root else None
        self.started = time.monotonic()
        for name in ('EVU_PERF_ACTIVE', 'EVU_PERF_RUN_ID', 'EVU_PERF_EVENTS_FILE'):
            self._old_env[name] = os.environ.get(name)
        os.environ['EVU_PERF_ACTIVE'] = '1'
        os.environ['EVU_PERF_RUN_ID'] = self.run_id
        os.environ['EVU_PERF_EVENTS_FILE'] = str(self.events_path)
        record_event('run_start', run_id=self.run_id, label=self.label,
                     root_pid=self.root_pid, host=os.uname().nodename if hasattr(os, 'uname') else 'unknown')
        self._sample()
        self._thread = threading.Thread(target=self._sample_loop, name='evu-perf-sampler')
        self._thread.daemon = True
        self._thread.start()
        return self

    def _sample_loop(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self):
        now = time.monotonic()
        processes = _tree_processes(self.root_pid, self.root_starttime, self.exclude_pids)
        ticks = sum(x['ticks'] for x in processes.values())
        rss = sum(x['rss_bytes'] for x in processes.values())
        read_bytes = sum(x['read_bytes'] for x in processes.values())
        write_bytes = sum(x['write_bytes'] for x in processes.values())
        elapsed = (now - self._previous_time) if self._previous_time is not None else 0.0
        cpu_delta = 0
        read_delta = write_delta = 0
        if self._previous is not None:
            for pid, item in processes.items():
                old = self._previous.get(pid)
                if old is not None and old['starttime'] == item['starttime']:
                    cpu_delta += max(0, item['ticks'] - old['ticks'])
                    read_delta += max(0, item['read_bytes'] - old['read_bytes'])
                    write_delta += max(0, item['write_bytes'] - old['write_bytes'])
                elif self._previous is not None:
                    # A newly observed child has accumulated these counters
                    # since its own start; include them once in run totals.
                    cpu_delta += max(0, item['ticks'])
                    read_delta += max(0, item['read_bytes'])
                    write_delta += max(0, item['write_bytes'])
        cpu_seconds = cpu_delta / float(_TICKS_PER_SECOND)
        self._cpu_seconds_cumulative += cpu_seconds
        self._io_read_cumulative += read_delta
        self._io_write_cumulative += write_delta
        cores = (cpu_delta / float(_TICKS_PER_SECOND) / elapsed) if elapsed > 0 else 0.0
        cpu_count = max(1, int(os.cpu_count() or 1))
        total_mem, available_mem = _host_memory()
        limits = _cgroup_limits(total_mem)
        try:
            affinity_count = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            affinity_count = cpu_count
        capacities = [float(x) for x in (affinity_count, limits.get('cpuset_cpu_count'),
                                          limits.get('cpu_quota_cores')) if x and x > 0]
        effective_cpu_capacity = min(capacities) if capacities else float(cpu_count)
        cgroup_mem_available = None
        if limits.get('memory_limit_bytes') is not None and limits.get('memory_current_bytes') is not None:
            cgroup_mem_available = max(0, limits['memory_limit_bytes'] - limits['memory_current_bytes'])
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
        sample = {
            'event_type': 'sample', 'utc': _utc_now(), 'elapsed_seconds': round(elapsed, 3),
            'process_count': len(processes), 'cpu_cores': round(cores, 3),
            'cpu_host_pct': round(100.0 * cores / cpu_count, 3),
            'effective_cpu_capacity': round(effective_cpu_capacity, 3),
            'cpu_capacity_pct': round(100.0 * cores / effective_cpu_capacity, 3),
            'cpu_affinity_count': affinity_count,
            'cgroup_cpu_quota_cores': limits.get('cpu_quota_cores'),
            'cgroup_cpuset_cpu_count': limits.get('cpuset_cpu_count'),
            'rss_bytes': rss, 'rss_gib': round(rss / float(1024 ** 3), 3),
            'rss_host_pct': round(100.0 * rss / total_mem, 3) if total_mem else None,
            'cgroup_memory_limit_gib': round(limits['memory_limit_bytes'] / float(1024 ** 3), 3)
                if limits.get('memory_limit_bytes') else None,
            'cgroup_memory_available_gib': round(cgroup_mem_available / float(1024 ** 3), 3)
                if cgroup_mem_available is not None else None,
            'rss_cgroup_pct': round(100.0 * rss / limits['memory_limit_bytes'], 3)
                if limits.get('memory_limit_bytes') else None,
            'read_bytes_total': read_bytes, 'write_bytes_total': write_bytes,
            'read_bytes_cumulative': self._io_read_cumulative,
            'write_bytes_cumulative': self._io_write_cumulative,
            'cpu_seconds_cumulative': round(self._cpu_seconds_cumulative, 3),
            'read_mb_s': round(read_delta / max(elapsed, 0.001) / 1048576.0, 3),
            'write_mb_s': round(write_delta / max(elapsed, 0.001) / 1048576.0, 3),
            'host_cpu_count': cpu_count,
            'host_mem_total_gib': round(total_mem / float(1024 ** 3), 3) if total_mem else None,
            'host_mem_available_gib': round(available_mem / float(1024 ** 3), 3) if available_mem else None,
            'host_load1': round(load1, 3) if load1 is not None else None,
        }
        self._samples.append(sample)
        _write_json_line(self.samples_path, sample)
        self._previous = processes
        self._previous_time = now

    def finish(self, exit_status=None, error=None):
        if not self._enabled:
            return None
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(2.0, self.interval + 1.0))
        self._sample()
        ended = time.monotonic()
        duration = max(0.0, ended - (self.started or ended))
        samples = self._samples
        cpu_values = [x['cpu_cores'] for x in samples
                      if x.get('elapsed_seconds', 0) > 0 and x.get('process_count', 0) > 0]
        rss_values = [x['rss_gib'] for x in samples if x.get('process_count', 0) > 0]
        latest = samples[-1] if samples else {}
        events = self._read_events()
        categories = {}
        transfers = {'count': 0, 'scp_count': 0, 'ssh_count': 0, 'bytes': 0,
                     'seconds': 0.0, 'scp_seconds': 0.0, 'failures': 0}
        queue_wait = {'count': 0, 'seconds': 0.0, 'max_seconds': 0.0}
        queue_wait_by_category = {}
        active_tasks = {}
        max_concurrent = {}
        concurrency_events = []
        for item in events:
            if item.get('event_type') == 'task_start':
                concurrency_events.append((float(item.get('epoch') or 0), 1,
                                           item.get('category', 'other'), item.get('task_id')))
            elif item.get('event_type') == 'task':
                concurrency_events.append((float(item.get('epoch') or 0), -1,
                                           item.get('category', 'other'), item.get('task_id')))
            elif item.get('event_type') == 'stage_start':
                concurrency_events.append((float(item.get('epoch') or 0), 1,
                                           item.get('stage', 'stage'), item.get('stage')))
            elif item.get('event_type') == 'stage_end':
                concurrency_events.append((float(item.get('epoch') or 0), -1,
                                           item.get('stage', 'stage'), item.get('stage')))
            elif item.get('event_type') == 'transfer_start':
                concurrency_events.append((float(item.get('epoch') or 0), 1,
                                           'transfer_' + item.get('operation', 'other'), item.get('transfer_id')))
            elif item.get('event_type') == 'transfer':
                concurrency_events.append((float(item.get('epoch') or 0), -1,
                                           'transfer_' + item.get('operation', 'other'), item.get('transfer_id')))
        for _, direction, category, task_id in sorted(concurrency_events, key=lambda x: (x[0], -x[1])):
            if direction > 0:
                active_tasks[category] = active_tasks.get(category, 0) + 1
                max_concurrent[category] = max(max_concurrent.get(category, 0), active_tasks[category])
            else:
                active_tasks[category] = max(0, active_tasks.get(category, 0) - 1)
        for item in events:
            event_type = item.get('event_type')
            if event_type == 'task':
                key = item.get('category', 'other')
                value = float(item.get('elapsed_seconds') or 0)
                group = categories.setdefault(key, {'count': 0, 'seconds': 0.0, 'max_seconds': 0.0,
                                                     'failures': 0, 'metrics': {}})
                group['count'] += 1
                group['seconds'] += value
                group['max_seconds'] = max(group['max_seconds'], value)
                if item.get('status') == 'failed':
                    group['failures'] += 1
                for metric, amount in (item.get('metrics') or {}).items():
                    group['metrics'][metric] = group['metrics'].get(metric, 0.0) + float(amount)
            elif event_type == 'stage_end':
                key = item.get('stage', 'stage')
                value = float(item.get('elapsed_seconds') or 0)
                group = categories.setdefault(key, {'count': 0, 'seconds': 0.0, 'max_seconds': 0.0,
                                                     'failures': 0, 'metrics': {}})
                group['count'] += 1
                group['seconds'] += value
                group['max_seconds'] = max(group['max_seconds'], value)
                if item.get('status') == 'failed':
                    group['failures'] += 1
            elif event_type in ('stage_queue', 'task_queue'):
                value = float(item.get('queue_wait_seconds') or 0)
                category = item.get('category', item.get('stage', 'stage'))
                group = queue_wait_by_category.setdefault(category,
                    {'count': 0, 'seconds': 0.0, 'max_seconds': 0.0})
                group['count'] += 1
                group['seconds'] += value
                group['max_seconds'] = max(group['max_seconds'], value)
                queue_wait['count'] += 1
                queue_wait['seconds'] += value
                queue_wait['max_seconds'] = max(queue_wait['max_seconds'], value)
            elif event_type == 'transfer':
                transfers['count'] += 1
                transfers['bytes'] += int(item.get('bytes') or 0)
                elapsed = float(item.get('elapsed_seconds') or 0)
                transfers['seconds'] += elapsed
                if item.get('status') != 'complete':
                    transfers['failures'] += 1
                if item.get('operation') == 'scp':
                    transfers['scp_count'] += 1
                    transfers['scp_seconds'] += elapsed
                elif item.get('operation') == 'ssh':
                    transfers['ssh_count'] += 1
        for group in categories.values():
            group['seconds'] = round(group['seconds'], 3)
            group['max_seconds'] = round(group['max_seconds'], 3)
            group['mean_seconds'] = round(group['seconds'] / max(1, group['count']), 3)
            group['metrics'] = {key: round(value, 3) for key, value in group['metrics'].items()}
        for key, group in categories.items():
            group['max_concurrent_tasks'] = max_concurrent.get(key, 0)
        transfers['seconds'] = round(transfers['seconds'], 3)
        transfers['scp_seconds'] = round(transfers['scp_seconds'], 3)
        transfers['mib'] = round(transfers['bytes'] / float(1024 ** 2), 3)
        transfers['mean_mib_s'] = round(transfers['mib'] / max(transfers['scp_seconds'], 0.001), 3)
        transfers['max_concurrent_scp'] = max_concurrent.get('transfer_scp', 0)
        transfers['max_concurrent_ssh'] = max_concurrent.get('transfer_ssh', 0)
        queue_wait['seconds'] = round(queue_wait['seconds'], 3)
        queue_wait['max_seconds'] = round(queue_wait['max_seconds'], 3)
        for group in queue_wait_by_category.values():
            group['seconds'] = round(group['seconds'], 3)
            group['max_seconds'] = round(group['max_seconds'], 3)
        raw_run_configuration = next((item for item in events if item.get('event_type') == 'run_config'), None)
        run_configuration = ({key: value for key, value in raw_run_configuration.items()
                              if key not in ('event_type', 'utc', 'epoch', 'pid')}
                             if raw_run_configuration else None)
        all_metrics = {}
        for group in categories.values():
            for metric, amount in group.get('metrics', {}).items():
                all_metrics[metric] = all_metrics.get(metric, 0.0) + float(amount)
        download_summary = {key: round(value, 3) for key, value in all_metrics.items()
                            if 'download' in key or 'http_' in key}
        compression_summary = {
            'compress_seconds': round(all_metrics.get('zlib_compress_seconds', 0.0), 3),
            'chunk_write_seconds': round(all_metrics.get('zarr_chunk_write_seconds', 0.0), 3),
            'input_mib': round(all_metrics.get('zlib_input_bytes', 0.0) / float(1024 ** 2), 3),
            'output_mib': round(all_metrics.get('zlib_output_bytes', 0.0) / float(1024 ** 2), 3),
        }
        summary = {
            'schema': 'omd-evu-performance-1', 'run_id': self.run_id,
            'label': self.label, 'command': self.command, 'root_pid': self.root_pid,
            'started_utc': events[0].get('utc') if events else None,
            'ended_utc': next((item.get('utc') for item in reversed(events)
                               if item.get('event_type') == 'run_end'), None),
            'duration_seconds': round(duration, 3), 'exit_status': exit_status,
            'error': error, 'sample_interval_seconds': self.interval,
            'sample_count': len(samples), 'host_cpu_count': latest.get('host_cpu_count'),
            'average_cpu_cores': round(sum(cpu_values) / max(1, len(cpu_values)), 3),
            'peak_cpu_cores': round(max(cpu_values) if cpu_values else 0.0, 3),
            'peak_cpu_host_pct': round(max((x.get('cpu_host_pct', 0) for x in samples), default=0), 3),
            'peak_cpu_capacity_pct': round(max((x.get('cpu_capacity_pct', 0) for x in samples), default=0), 3),
            'effective_cpu_capacity': latest.get('effective_cpu_capacity'),
            'peak_process_tree_rss_gib': round(max(rss_values) if rss_values else 0.0, 3),
            'average_process_tree_rss_gib': round(sum(rss_values) / max(1, len(rss_values)), 3),
            'peak_process_count': max((x.get('process_count', 0) for x in samples), default=0),
            'process_tree_cpu_seconds': round(self._cpu_seconds_cumulative, 3),
            'process_tree_read_bytes': self._io_read_cumulative,
            'process_tree_write_bytes': self._io_write_cumulative,
            'peak_read_mb_s': round(max((x.get('read_mb_s', 0) for x in samples), default=0), 3),
            'peak_write_mb_s': round(max((x.get('write_mb_s', 0) for x in samples), default=0), 3),
            'host_memory_total_gib': latest.get('host_mem_total_gib'),
            'cgroup_memory_limit_gib': latest.get('cgroup_memory_limit_gib'),
            'minimum_cgroup_memory_available_gib': min((x.get('cgroup_memory_available_gib') for x in samples
                                                          if x.get('cgroup_memory_available_gib') is not None), default=None),
            'minimum_host_memory_available_gib': min((x.get('host_mem_available_gib') for x in samples
                                                       if x.get('host_mem_available_gib') is not None), default=None),
            'run_configuration': run_configuration,
            'download_summary': download_summary,
            'compression_summary': compression_summary,
            'stage_task_summary': categories, 'stage_queue_wait': queue_wait,
            'queue_wait_by_category': queue_wait_by_category,
            'transfer_summary': transfers,
            'samples_file': self.samples_path.name if self.samples_path else None,
            'events_file': self.events_path.name if self.events_path else None,
            'notes': ['CPU and process-tree RSS/IO cover this run and descendants visible in /proc.',
                      'RSS is summed across processes and can count shared pages more than once.',
                      'Disk I/O is process-attributed where Linux /proc exposes it; network byte counts are instrumented transfers.']
        }
        try:
            self.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            self.summary_path.chmod(0o640)
            lines = [
                'OMD EVU Converter performance summary',
                'Run: ' + str(self.run_id),
                'Label: ' + str(self.label),
                'Duration: %.1f seconds (%.1f minutes)' % (duration, duration / 60.0),
                'Exit status: ' + str(exit_status),
                'Samples: %d at %.1f-second intervals' % (len(samples), self.interval),
                'CPU: average %.2f cores; peak %.2f cores (%.1f%% of host; %.1f%% of effective CPU capacity)' %
                    (summary['average_cpu_cores'], summary['peak_cpu_cores'], summary['peak_cpu_host_pct'],
                     summary['peak_cpu_capacity_pct']),
                'Process-tree RSS: peak %.2f GiB; average %.2f GiB (summed RSS; shared pages may be counted more than once)' %
                    (summary['peak_process_tree_rss_gib'], summary['average_process_tree_rss_gib']),
                'Effective CPU capacity: %.2f cores; cgroup memory limit: %s GiB' %
                    (summary['effective_cpu_capacity'] or 0.0,
                     'unlimited/unknown' if summary['cgroup_memory_limit_gib'] is None else '%.2f' % summary['cgroup_memory_limit_gib']),
                'Peak process count: %d' % summary['peak_process_count'],
                'Process-tree CPU time: %.1f core-seconds' % summary['process_tree_cpu_seconds'],
                'Compression: %.1fs CPU time; %.2f MiB input -> %.2f MiB output; chunk writes %.1fs' %
                    (compression_summary['compress_seconds'], compression_summary['input_mib'],
                     compression_summary['output_mib'], compression_summary['chunk_write_seconds']),
                'Process-tree disk I/O: read %.2f GiB; written %.2f GiB' %
                    (summary['process_tree_read_bytes'] / float(1024 ** 3),
                     summary['process_tree_write_bytes'] / float(1024 ** 3)),
                'Measured SSH/SCP attempts: %d SSH + %d SCP (%d failures); %.2f MiB uploaded; %.2f MiB/s SCP average' %
                    (transfers['ssh_count'], transfers['scp_count'], transfers['failures'],
                     transfers['mib'], transfers['mean_mib_s']),
                'Peak concurrent transfers: %d SCP + %d SSH' %
                    (transfers['max_concurrent_scp'], transfers['max_concurrent_ssh']),
                'Observed stage/task queue wait: %.1fs total; %.1fs maximum' %
                    (queue_wait['seconds'], queue_wait['max_seconds']),
                'Stage/task totals:'
            ]
            if run_configuration:
                lines.insert(4, 'Run configuration: ' + json.dumps(run_configuration, sort_keys=True))
            if download_summary:
                lines.append('Download counters: ' + ', '.join('%s=%s' % (k, v) for k, v in sorted(download_summary.items())))
            for key, value in sorted(categories.items()):
                lines.append('  %-28s %4d tasks  total %8.1fs  mean %6.2fs  max %7.1fs  peak concurrent %d  failures %d' %
                             (key, value['count'], value['seconds'], value['mean_seconds'],
                              value['max_seconds'], value.get('max_concurrent_tasks', 0), value['failures']))
                if value.get('metrics'):
                    lines.append('    metrics: ' + ', '.join('%s=%s' % (k, v) for k, v in sorted(value['metrics'].items())))
            lines.extend(['', 'Detailed samples: ' + str(self.samples_path),
                          'Detailed events: ' + str(self.events_path), ''])
            self.text_path.write_text('\n'.join(lines), encoding='utf-8')
            self.text_path.chmod(0o640)
        except Exception:
            pass
        return summary

    def _read_events(self):
        result = []
        try:
            with self.events_path.open('r', encoding='utf-8') as stream:
                for line in stream:
                    try:
                        result.append(json.loads(line))
                    except ValueError:
                        continue
        except (IOError, OSError):
            pass
        return result

    def __exit__(self, exc_type, exc_value, exc_tb):
        status = 1 if exc_type else (self._exit_status if self._exit_status is not None else 0)
        error = (str(exc_value) if exc_value else None)
        record_event('run_end', exit_status=status, error=error)
        self.finish(status, error)
        for name, old in self._old_env.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        return False


def watch_pid(pid, label, log_dir, interval, retention_days):
    pid = int(pid)
    initial = _read_proc(pid)
    if initial is None:
        return 0
    monitor = PerformanceMonitor(label, log_dir, interval, retention_days,
                                 root_pid=pid, exclude_pids=(os.getpid(),))
    monitor.__enter__()
    stop = threading.Event()
    def stop_handler(signum, frame):
        stop.set()
    old_term = signal.signal(signal.SIGTERM, stop_handler)
    old_int = signal.signal(signal.SIGINT, stop_handler)
    try:
        while not stop.wait(1.0):
            current = _read_proc(pid)
            if current is None or current['starttime'] != initial['starttime']:
                break
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        reason = 'external process ended' if not stop.is_set() else 'monitor stopped by wrapper'
        record_event('run_end', exit_status=None, reason=reason)
        monitor.finish(None, reason)
        for name, old in monitor._old_env.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch-pid', type=int, required=True)
    parser.add_argument('--label', default='cron')
    parser.add_argument('--log-dir', required=True)
    parser.add_argument('--interval', type=float, default=5.0)
    parser.add_argument('--retention-days', type=int, default=7)
    args = parser.parse_args(argv)
    return watch_pid(args.watch_pid, args.label, args.log_dir, args.interval, args.retention_days)


if __name__ == '__main__':
    sys.exit(main())
