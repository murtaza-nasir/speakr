import json
from pathlib import Path
import tempfile
import unittest
import hashlib
from unittest.mock import patch
from types import SimpleNamespace

from test_bridge import Clock
from common import atomic_json
from watchdog import Watchdog, WindowsRuntime


class Runtime:
    def __init__(self):
        self.calls, self.notifications = [], []
        self.data = {'docker_ready': True, 'containers_absent': False,
                     'speakr': {'ready': True}, 'asr': {'ready': True}, 'ollama': {'ready': True},
                     'bridge': {'enabled': True, 'state': 'Running', 'process_matches': True},
                     'ollama_task': {'enabled': True, 'state': 'Running'}}
    def observe(self): return self.data
    def action(self, component, verb, heartbeat=None): self.calls.append((component, verb))
    def notify(self, recovery=False): self.notifications.append(recovery)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.clock, self.runtime = Path(self.tmp.name), Clock(), Runtime()
        self.watch = Watchdog({}, self.root, self.runtime, self.clock)
        self.heartbeat()
    def heartbeat(self, **kwargs):
        atomic_json(self.root / 'heartbeat.json', {'pid': 123, 'start_marker': 'abc',
                    'updated': self.clock(), 'operation': 'ffmpeg', 'deadline': self.clock() + 7200, **kwargs})
    def test_stopped_ollama_starts_once_cooldown_persists(self):
        self.runtime.data['ollama']['ready'] = False
        self.runtime.data['ollama_task']['state'] = 'Ready'
        self.watch.tick()
        Watchdog({}, self.root, self.runtime, self.clock).tick()
        self.assertEqual(self.runtime.calls, [('ollama', 'Start')])
    def test_long_media_and_inference_never_killed_for_stale_cycle(self):
        self.runtime.data['ollama']['ready'] = False
        self.clock.advance(600)
        self.heartbeat()
        self.watch.tick()
        self.assertEqual(self.runtime.calls, [])
    def test_running_ollama_unknown_busy_is_degraded_not_killed(self):
        self.runtime.data['ollama']['ready'] = False
        for _ in range(4):
            self.clock.advance(60)
            self.watch.tick()
        self.assertEqual(self.runtime.calls, [])
    def test_maintenance_blocks_all_actions(self):
        self.runtime.data['docker_ready'] = False
        atomic_json(self.root / 'maintenance.json', {'paused': True})
        self.assertTrue(self.watch.tick()['maintenance'])
        self.assertEqual(self.runtime.calls, [])
    def test_disabled_worker_not_resurrected(self):
        self.runtime.data['bridge'].update(enabled=False, state='Disabled', process_matches=False)
        self.watch.tick()
        self.assertEqual(self.runtime.calls, [])
    def test_missing_worker_restarted_but_live_manual_worker_not_duplicated(self):
        self.runtime.data['bridge'].update(state='Ready', process_matches=False)
        self.watch.tick()
        self.assertEqual(self.runtime.calls, [('bridge', 'Start')])
        self.runtime.data['bridge']['process_matches'] = True
        self.clock.advance(121)
        self.watch.tick()
        self.assertEqual(len(self.runtime.calls), 1)
    def test_notifications_once_per_outage_and_recovery(self):
        self.runtime.data['asr']['ready'] = False
        self.watch.tick()
        self.clock.advance(601)
        self.watch.tick()
        self.watch.tick()
        self.runtime.data['asr']['ready'] = True
        self.heartbeat()
        self.watch.tick()
        self.watch.tick()
        self.assertEqual(self.runtime.notifications, [False, True])
    def test_restart_budget_survives_watchdog_recreation(self):
        self.runtime.data['ollama']['ready'] = False
        self.runtime.data['ollama_task']['state'] = 'Ready'
        for _ in range(5):
            Watchdog({}, self.root, self.runtime, self.clock).tick()
            self.clock.advance(121)
        self.assertEqual(len(self.runtime.calls), 3)
    def test_expired_dead_heartbeat_first_graceful_then_verified_stop(self):
        self.heartbeat(updated=0, deadline=self.clock() - 120)
        self.watch.tick()
        self.assertTrue((self.root / 'stop.json').exists())
        self.assertEqual(self.runtime.calls, [])
        self.clock.advance(121)
        self.watch.tick()
        self.assertEqual(self.runtime.calls, [('bridge', 'StopVerified')])


class ContainerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.compose = self.root / 'compose.yaml'
        self.compose.write_text('services: {}')
        self.cfg = {'docker': 'docker', 'compose_file': str(self.compose),
                    'runtime_manifest': {'compose_sha256': hashlib.sha256(self.compose.read_bytes()).hexdigest(),
                        'containers': {'app': {'image_id': 'sha256:app', 'image_ref': 'app@sha256:pinned', 'service': 'app'},
                                       'asr': {'image_id': 'sha256:asr', 'image_ref': 'asr@sha256:pinned', 'service': 'whisperx-asr'}}}}
        self.runtime = WindowsRuntime(self.cfg, self.root)

    def test_stopped_service_only_started_and_running_peer_not_recreated(self):
        def inspect(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b'sha256:app true' if args[-1] == 'app' else b'sha256:asr false')
        with patch('watchdog.subprocess.run', side_effect=inspect), patch('watchdog.subprocess.Popen') as start:
            self.runtime.action('containers', 'Start')
            self.assertEqual(start.call_args.args[0], ['docker', 'start', 'asr'])

    def test_compose_drift_causes_no_process_action(self):
        self.compose.write_text('changed')
        with patch('watchdog.subprocess.Popen') as start:
            with self.assertRaisesRegex(RuntimeError, 'Compose drift'):
                self.runtime.action('containers', 'Start')
            start.assert_not_called()

    def test_retagged_image_cannot_recreate_missing_container(self):
        def inspect(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b'sha256:other') if args[1] == 'image' else SimpleNamespace(returncode=1, stdout=b'')
        with patch('watchdog.subprocess.run', side_effect=inspect), patch('watchdog.subprocess.Popen') as start:
            with self.assertRaisesRegex(RuntimeError, 'Local image drift'):
                self.runtime.action('containers', 'Start')
            start.assert_not_called()

    def test_missing_verified_container_never_recreates_peer(self):
        def inspect(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b'sha256:app') if args[1] == 'image' else SimpleNamespace(returncode=1, stdout=b'')
        with patch('watchdog.subprocess.run', side_effect=inspect), patch('watchdog.subprocess.Popen') as start:
            self.runtime.action('containers', 'Start')
            args = start.call_args.args[0]
            self.assertIn('--no-recreate', args)
            self.assertIn('--no-deps', args)
            self.assertNotIn('whisperx-asr', args)


if __name__ == '__main__': unittest.main()
