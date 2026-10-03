"""Per-match adapter for isolated kernel collection and conservative evidence."""
import json
import os
from pathlib import Path
import threading
import time

from kernel_observer import KernelObserver
from kernel_evidence import EvidenceTracker


class MatchObserver:
    def __init__(self, match_id, contestants, output_dir, on_change, cancel_event=None):
        self.match_id = match_id
        self.contestants = contestants
        self.output_dir = Path(output_dir)
        self.on_change = on_change
        self.cancel_event = cancel_event
        self.lock = threading.RLock()
        self.tracker = None
        self.collector = None
        self.pending = []
        self.records = 0
        self.failed = False
        self.closed = False
        self.state = {'status': 'starting', 'message': 'Starting kernel observer'}
        self.path = self.output_dir / (match_id + '.kernel.jsonl')
        self.path.touch(mode=0o600, exist_ok=True)

    def _record(self, row):
        if self.records >= 250000:
            self.failed = True
            self.state.update(status='degraded', message='Kernel recording limit reached; attribution unavailable')
            return
        try:
            with self.path.open('a') as output:
                output.write(json.dumps(row) + '\n')
            self.records += 1
        except OSError:
            self.failed = True
            self.state.update(status='degraded', message='Cannot save kernel evidence; attribution unavailable')

    def _status(self, update):
        with self.lock:
            if self.closed:
                return
            previous = self.state.get('status')
            self.state.update(update)
            if update.get('status') in ('degraded', 'unavailable') or update.get('loss_count', 0):
                self.failed = True
            if self.failed and self.state.get('status') == 'ready':
                self.state.update(status='degraded', message='Trace coverage interrupted; attribution unavailable')
            if update.get('status') == 'stopped':
                self.state['previous_status'] = previous
            self._record({'record': 'observer_status', 'recorded_at': time.time(), **self.state})
            if self.tracker:
                self.tracker.set_health(dict(self.state))
        self.on_change()

    def _event(self, event):
        with self.lock:
            if self.closed:
                return
            self._record({'record': 'kernel', 'recorded_at': time.time(), **event})
            if self.tracker:
                self.tracker.ingest(event)
                self.tracker.set_health(dict(self.state))
                self._check_tracker()
            elif len(self.pending) < 10000:
                self.pending.append(event)
            else:
                self.failed = True
                self.state.update(status='degraded', message='Kernel startup buffer overflow; attribution unavailable')
        # The dashboard receives status changes; reports are settled on its
        # referee tick, rather than doing expensive UI work per kernel event.

    def _check_tracker(self):
        if self.tracker and self.tracker.status.get('status') == 'degraded':
            self.failed = True
            self.state.update(status='degraded', message=self.tracker.status.get('message') or 'Kernel evidence incomplete')

    def start(self):
        if os.getenv('ARENA_OBSERVER', '1') == '0':
            self._status({'status': 'disabled', 'message': 'Kernel observer disabled by ARENA_OBSERVER=0'})
            return
        try:
            self.collector = KernelObserver(self.match_id, self.contestants,
                                           self.output_dir / (self.match_id + '.observer'),
                                           self._event, self._status, cancel_event=self.cancel_event)
            identities = self.collector.start()
            if not identities:
                self._status({'status': 'unavailable', 'message': 'Kernel identities unavailable; using log evidence'})
                return
            with self.lock:
                self.tracker = EvidenceTracker(identities)
                for row in self.pending:
                    self.tracker.ingest(row)
                self.pending.clear()
                self.tracker.set_health(dict(self.state))
                self._check_tracker()
                self._record({'record': 'identities', 'recorded_at': time.time(), 'contestants': identities})
        except Exception as exc:
            self._status({'status': 'unavailable', 'message': f'Kernel observer could not start ({type(exc).__name__}); using log evidence'})
            if self.collector:
                self.collector.stop()
        self.on_change()

    def status(self):
        # Collector status refresh includes the heartbeat/dead-reader check.
        if self.collector and not self.closed:
            current = self.collector.status()
            with self.lock:
                if not self.failed:
                    self.state.update(current)
                if current.get('status') in ('unavailable', 'degraded') or current.get('loss_count', 0):
                    self.failed = True
                    self.state.update(current)
                if self.tracker:
                    self.tracker.set_health(dict(self.state))
                    self._check_tracker()
        with self.lock:
            return dict(self.state)

    def explain(self, player, info):
        self.status()
        with self.lock:
            if self.closed or self.failed or self.state.get('status') != 'ready' or not self.tracker:
                return None
            return self.tracker.explain(player, death_at=info.get('death_at'),
                allow_descendants=info.get('error_phase') == 'failed' and info.get('error_category') == 'model_signal')

    def stop(self):
        if self.collector:
            self.collector.stop()
        with self.lock:
            if self.state.get('status') not in ('disabled', 'unavailable', 'stopped'):
                previous = self.state['status']
                self.state.update(status='stopped', previous_status=previous, message='Kernel observation ended')
                self._record({'record': 'observer_status', 'recorded_at': time.time(), **self.state})
            self.closed = True
        self.on_change()
