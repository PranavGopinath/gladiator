import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from kernel_observer import KernelObserver, parse_event


class KernelObserverTests(unittest.TestCase):
    def test_wire_port_and_kernel_exit_are_preserved(self):
        event = parse_event('CONNECT ts=100 pid=12 start=80 namespace=7 local=172.1.0.2 local_port=345 remote=172.1.0.3 remote_port_net=' + str(socket.htons(22)) + ' result=0\n')
        self.assertEqual(event['remote_port'], 22)
        self.assertEqual(event['ts_ns'], 100)
        self.assertEqual(event['pidns'], 7)
        event = parse_event('EXIT ts=101 pid=12 start=80 exit_code=1792 group_exit_code=0\n')
        self.assertEqual(event['exit_code'], 7 << 8)
        self.assertIsNone(parse_event('Attaching 6 probes...\n'))
        with self.assertRaises(ValueError):
            parse_event('EXIT pid=12\n')

    def test_ready_then_loss_irrevocably_revokes_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            statuses = []
            observer = KernelObserver('test', {}, tmp, on_status=statuses.append)
            observer._consume('READY ts=1\n')
            observer._read(io.StringIO('Lost 7 events\n'), 'trace.stderr')
            observer._consume('READY ts=10\n')
            self.assertEqual(observer.status()['status'], 'degraded')
            self.assertEqual(observer.status()['loss_count'], 7)
            self.assertEqual([s['status'] for s in statuses], ['ready', 'degraded'])

    def test_monotonic_heartbeat_and_process_exit_revoke_health(self):
        observer = KernelObserver('test', {}, '.', heartbeat_timeout=5)
        observer._consume('READY ts=1\n')
        observer._last_heartbeat = 1
        observer._stop = Mock()
        observer._stop.wait.return_value = False
        observer._stop.is_set.return_value = False
        with patch('kernel_observer.time.monotonic', return_value=10):
            observer._watch()
        self.assertEqual(observer.status()['status'], 'degraded')
        self.assertTrue(observer._ready.is_set())
        observer = KernelObserver('test', {}, '.')
        observer._process = Mock()
        observer._process.poll.return_value = 1
        observer._stop = Mock()
        observer._stop.wait.return_value = False
        observer._stop.is_set.return_value = False
        observer._watch()
        self.assertEqual(observer.status()['status'], 'degraded')

    def test_events_get_boot_epoch_and_unique_ids(self):
        events = []
        observer = KernelObserver('test', {}, '.', on_event=events.append)
        observer.boot_id = 'boot-a'
        for _ in range(2):
            observer._consume('SIGNAL ts=10 sender=1 sender_start=2 target=3 target_start=4 signal=15 kernel_generated=0 signal_code=0\n')
        self.assertEqual(events[0]['boot_id'], 'boot-a')
        self.assertEqual(events[0]['epoch'], observer.epoch)
        self.assertNotEqual(events[0]['id'], events[1]['id'])
        self.assertEqual(events[0]['target_start'], 4)
        self.assertEqual(events[0]['kernel_generated'], 0)
        self.assertEqual(events[0]['signal_code'], 0)

    def test_start_registers_supervisor_and_only_privileges_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            observer = KernelObserver('test', {'a': {'container_id': 'container-a', 'tracked_pid': 101, 'supervisor_pid': 99}}, tmp)
            identities = {str(pid): dict(pid=pid, namespace=7, boot_id='boot-a', start_min_ns=1, start_max_ns=2) for pid in (99, 101)}
            command = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(identities)))
            worker = Mock()
            worker.start.side_effect = lambda: observer._consume('READY ts=1\n')
            with patch.object(observer, '_ensure_image'), patch.object(observer, '_command', command), patch('kernel_observer.subprocess.Popen'), patch('kernel_observer.threading.Thread', return_value=worker):
                result = observer.start()
                run_args = next(c.args[0] for c in command.call_args_list if c.args[0][0] == 'run')
                self.assertIn('--privileged', run_args)
                self.assertIn('--pid=host', run_args)
                self.assertIn('--network=none', run_args)
                self.assertFalse(any('docker.sock' in arg or '--volume' in arg or '--mount' in arg for arg in run_args))
                self.assertEqual(result['a']['supervisor']['pid'], 99)
                trace = (Path(tmp) / 'trace.bt').read_text()
                self.assertIn('$ns == 7', trace)
                self.assertNotIn('__NAMESPACE_FILTER__', trace)
                observer.stop()
                remove_args = next(c.args[0] for c in command.call_args_list if c.args[0][0] == 'rm')
                self.assertEqual(remove_args, ['rm', '-f', observer.name])

    def test_start_failure_is_optional_and_cleanup_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            statuses = []
            observer = KernelObserver('test', {'a': {'container_id': 'a', 'tracked_pid': 1}}, tmp, on_status=statuses.append)
            with patch.object(observer, '_ensure_image', side_effect=FileNotFoundError('docker')):
                self.assertEqual(observer.start(), {})
            self.assertEqual(observer.status()['status'], 'unavailable')
            self.assertIn('docker', statuses[-1]['message'])

    def test_cancelled_start_does_not_create_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = Mock()
            cancel.is_set.return_value = True
            observer = KernelObserver('test', {'a': {'container_id': 'a', 'tracked_pid': 1}}, tmp, cancel_event=cancel)
            with patch.object(observer, '_ensure_image') as build, patch.object(observer, '_command') as command:
                self.assertEqual(observer.start(), {})
                build.assert_not_called()
                command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
