"""Bounded, optional Docker Desktop/Linux kernel observer for arena matches.

Only the separate observer gets host PID access and privileges. Its image is
built once, then reused; no Docker socket or host mounts are exposed to it.
Kernel probe compatibility is established by actual READY, never assumed.
"""
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid


IMAGE = 'agent-arena-kernel-observer:local'
SOURCE = Path(__file__).resolve().parent / 'observer'
KINDS = {'CONNECT', 'SSH_RECV', 'FORK', 'SIGNAL', 'EXIT', 'READY', 'HEARTBEAT'}
LOSS = re.compile(r'\b(?:lost|dropped)\s+(\d+)\s+(?:events|samples)', re.I)
FIELDS = re.compile(r'(\w+)=([^\s]+)')
_BUILD_LOCK = threading.Lock()


def parse_event(line):
    """Parse the bounded kernel wire format, including network byte order."""
    kind = line.split(' ', 1)[0].strip()
    if kind not in KINDS:
        return None
    event = {'kind': kind}
    for key, value in FIELDS.findall(line):
        if key in {'local', 'remote', 'comm'}:
            event[key] = value
        else:
            event[key] = int(value)
    if 'ts' not in event:
        raise ValueError('kernel event has no timestamp')
    event['ts_ns'] = event['ts']
    if 'namespace' in event:
        event['pidns'] = event['namespace']
    if 'remote_port_net' in event:
        event['remote_port'] = socket.ntohs(event['remote_port_net'])
    return event


class KernelObserver:
    """start() returns contestant identities; unavailable tracing returns {}.

    contestants maps id to {container_id, tracked_pid, supervisor_pid?}; PIDs
    are host PIDs in Docker's Linux VM. Callbacks receive normalized event and
    status dictionaries. Health loss is terminal for this collector epoch.
    """

    def __init__(self, match_id, contestants, output_dir, on_event=None,
                 on_status=None, *, ready_timeout=25, heartbeat_timeout=5, cancel_event=None):
        self.match_id = str(match_id)
        self.contestants = dict(contestants)
        self.output_dir = Path(output_dir)
        self.on_event = on_event or (lambda event: None)
        self.on_status = on_status or (lambda status: None)
        self.ready_timeout = ready_timeout
        self.heartbeat_timeout = heartbeat_timeout
        self.epoch = uuid.uuid4().hex
        self.name = 'arena-observer-' + self.epoch[:16]
        self.boot_id = None
        self.identities = {}
        self._state = 'unavailable'
        self.cancel_event = cancel_event
        self._status_payload = dict(status='unavailable', epoch=self.epoch, boot_id=None, loss_count=0)
        self.loss_count = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_heartbeat = None
        self._process = None
        self._threads = []
        self._container_created = False
        self._started = False
        self._sequence = 0

    def _command(self, args, *, timeout=15, input=None):
        # Explicit Docker CLI, current host context; never modify docker context.
        return subprocess.run(['docker', *args], input=input, capture_output=True,
                              text=True, check=True, timeout=timeout)

    def _status(self, status, message, **extra):
        with self._lock:
            if self._state == 'degraded' and status == 'ready':
                return
            self._state = status
            event = dict(status=status, message=message, epoch=self.epoch,
                         boot_id=self.boot_id, loss_count=self.loss_count,
                         received_at=time.time(), **extra)
            self._status_payload = event
        try:
            self.on_status(event)
        except Exception:
            # A UI consumer exception must not leave the observer unmanaged.
            if status == 'ready':
                with self._lock:
                    self._state = 'degraded'

    def status(self):
        with self._lock:
            result = dict(self._status_payload)
            result['status'] = self._state
            result['loss_count'] = self.loss_count
        result['heartbeat_age'] = (None if self._last_heartbeat is None else
                                   time.monotonic() - self._last_heartbeat)
        return result

    def _cancelled(self):
        return self._stop.is_set() or bool(self.cancel_event and self.cancel_event.is_set())

    def _check_cancel(self):
        if self._cancelled():
            raise RuntimeError('observer startup cancelled')

    def _degrade(self, message):
        if not self._stop.is_set() and self._state != 'degraded':
            self._status('degraded', message)

    def _ensure_image(self):
        # Rebuild cached layers after an observer package upgrade, rather than
        # accidentally running an old identity reader under the same image tag.
        digest = hashlib.sha256()
        for filename in ('Dockerfile', 'identity.py'):
            digest.update((SOURCE / filename).read_bytes())
        source_hash = digest.hexdigest()
        with _BUILD_LOCK:
            try:
                cached = self._command(['image', 'inspect', '--format',
                                       '{{index .Config.Labels "arena.observer.source"}}',
                                       IMAGE], timeout=10).stdout.strip()
                if cached == source_hash:
                    return
            except subprocess.CalledProcessError:
                pass
            # Build output goes straight to disk, never an unbounded capture.
            with (self.output_dir / 'observer-build.log').open('w') as log:
                subprocess.run(['docker', 'build', '--tag', IMAGE, '--label',
                                'arena.observer.source=' + source_hash, str(SOURCE)],
                               stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=240)

    def start(self):
        if self._started:
            return self.identities if self._state == 'ready' else {}
        self._started = True
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if not self.contestants:
                raise ValueError('no contestant identities to observe')
            self._check_cancel()
            self._ensure_image()
            self._check_cancel()
            self._command(['run', '-d', '--name', self.name,
                           '--label', 'arena.kernel_observer=' + self.epoch,
                           '--privileged', '--pid=host', '--network=none',
                           '--cpus=0.5', '--memory=384m', '--memory-swap=384m',
                           '--pids-limit=64', '--log-driver=none', IMAGE])
            self._container_created = True
            self._check_cancel()
            pids = set()
            for contestant in self.contestants.values():
                pids.add(int(contestant['tracked_pid']))
                if contestant.get('supervisor_pid'):
                    pids.add(int(contestant['supervisor_pid']))
            if any(pid <= 0 for pid in pids):
                raise ValueError('tracked host PIDs must be positive')
            proc_identities = json.loads(self._command(
                ['exec', self.name, 'python3', '/opt/observer/identity.py',
                 *map(str, sorted(pids))]).stdout)
            for cid, contestant in self.contestants.items():
                identity = dict(proc_identities[str(contestant['tracked_pid'])])
                identity.update(container_id=contestant['container_id'], epoch=self.epoch)
                if contestant.get('supervisor_pid'):
                    supervisor = dict(proc_identities[str(contestant['supervisor_pid'])])
                    supervisor['epoch'] = self.epoch
                    if supervisor['namespace'] != identity['namespace']:
                        raise ValueError('tracked process and supervisor namespace mismatch')
                    identity['supervisor'] = supervisor
                self.identities[cid] = identity
            boots = {identity['boot_id'] for identity in self.identities.values()}
            if len(boots) != 1:
                raise ValueError('contestants have inconsistent kernel boot identities')
            self.boot_id = boots.pop()
            namespaces = sorted({identity['namespace'] for identity in self.identities.values()})
            expression = ' || '.join('$ns == ' + str(ns) for ns in namespaces)
            template = (SOURCE / 'trace.bt').read_text()
            trace = template.replace('__NAMESPACE_FILTER__', expression)
            (self.output_dir / 'trace.bt').write_text(trace)
            (self.output_dir / 'identities.json').write_text(json.dumps(self.identities, indent=2))
            self._command(['exec', '-i', self.name, 'sh', '-c', 'cat > /tmp/trace.bt'], input=trace)
            self._check_cancel()
            # BPF maps bounded independently of contestant-generated event volume.
            self._process = subprocess.Popen(
                ['docker', 'exec', '-e', 'BPFTRACE_MAP_KEYS_MAX=16384',
                 '-e', 'BPFTRACE_PERF_RB_PAGES=256', self.name,
                 'bpftrace', '-B', 'line', '/tmp/trace.bt'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1)
            self._last_heartbeat = time.monotonic()
            for stream, filename in ((self._process.stdout, 'kernel-events.log'),
                                     (self._process.stderr, 'trace.stderr')):
                worker = threading.Thread(target=self._read, args=(stream, filename), daemon=True)
                self._threads.append(worker)
                worker.start()
            watchdog = threading.Thread(target=self._watch, daemon=True)
            self._threads.append(watchdog)
            watchdog.start()
            deadline = time.monotonic() + self.ready_timeout
            while not self._ready.wait(0.1) and time.monotonic() < deadline:
                self._check_cancel()
            self._check_cancel()
            if not self._ready.is_set() or self._state != 'ready':
                raise RuntimeError('kernel probes did not become healthy before startup deadline')
            return self.identities
        except Exception as exc:
            self._status('unavailable', 'Kernel observer unavailable: ' + str(exc))
            self._cleanup()
            return {}

    def _read(self, stream, filename):
        # Rotate bounded raw evidence rather than accumulating events in memory.
        path = self.output_dir / filename
        size = 0
        log = None
        try:
            log = path.open('w')
            while True:
                line = stream.readline(16_385)
                if not line:
                    break
                if size + len(line.encode('utf-8')) > 16 * 1024 * 1024:
                    log.close()
                    for n in (2, 1):
                        older = Path(str(path) + '.' + str(n))
                        if older.exists():
                            older.replace(Path(str(path) + '.' + str(n + 1)))
                    path.replace(Path(str(path) + '.1'))
                    log = path.open('w')
                    size = 0
                log.write(line)
                log.flush()
                size += len(line.encode('utf-8'))
                if len(line) > 16_384 or not line.endswith('\n'):
                    self._degrade('Malformed or truncated kernel event')
                    continue
                lost = LOSS.search(line)
                if lost:
                    with self._lock:
                        self.loss_count += int(lost.group(1))
                    self._degrade('Kernel collector lost events')
                    continue
                if filename == 'trace.stderr':
                    if re.search(r'\b(?:ERROR|failed|cannot|dropped|lost)\b', line, re.I):
                        self._degrade('Kernel collector reported an error')
                    continue
                try:
                    self._consume(line)
                except Exception:
                    self._degrade('Kernel event parsing or consumer failed')
        except Exception:
            self._degrade('Kernel evidence stream failed')
        finally:
            if log:
                log.close()
            stream.close()
            self._degrade('Kernel collector stream ended')

    def _consume(self, line):
        event = parse_event(line)
        if event is None:
            return
        if event['kind'] in {'READY', 'HEARTBEAT'}:
            if self._stop.is_set():
                return
            self._last_heartbeat = time.monotonic()
            with self._lock:
                self._status_payload['kernel_ts_ns'] = event['ts']
            if event['kind'] == 'READY':
                self._status('ready', 'Kernel observer tracing contestants', kernel_ts_ns=event['ts'])
                self._ready.set()
            return
        self._sequence += 1
        event.update(id=self.epoch + ':' + str(self._sequence), epoch=self.epoch,
                     boot_id=self.boot_id, received_at=time.time())
        self.on_event(event)

    def _watch(self):
        while not self._stop.wait(0.25):
            if self._process and self._process.poll() is not None:
                self._degrade('Kernel collector exited')
                self._ready.set()
                return
            if (self._last_heartbeat is not None and
                    time.monotonic() - self._last_heartbeat > self.heartbeat_timeout):
                self._degrade('Kernel observer heartbeat timed out')
                self._ready.set()
                return

    def _cleanup(self):
        self._stop.set()
        if self._container_created:
            try:
                self._command(['exec', self.name, 'pkill', '-INT', '-x', 'bpftrace'], timeout=3)
            except Exception:
                pass
            # Remove only the exact container created by this observer instance.
            try:
                self._command(['rm', '-f', self.name], timeout=5)
                self._container_created = False
            except Exception:
                # A retryable cleanup failure must not claim the container is
                # gone. Best effort kill also stops probes when remove failed.
                try:
                    self._command(['kill', self.name], timeout=3)
                except Exception:
                    pass
        if self._process:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)

    def stop(self):
        self._cleanup()
        message = 'Kernel observer stopped'
        if self._container_created:
            message += '; observer container cleanup did not complete'
        self._status('stopped', message, cleanup_complete=not self._container_created)
