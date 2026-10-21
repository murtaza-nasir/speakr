"""One bounded supervision pass. Never infer permission to stop a busy GPU job."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from bridge import WorkerLock
from common import CREATE_NO_WINDOW, ROOT, atomic_json, config, maintenance_paused
from health import probe_http, probe_llm


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


class WindowsRuntime:
    def __init__(self, cfg, root):
        self.cfg, self.root = cfg, Path(root)

    def command(self, action, **params):
        args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                str(self.root / 'runtime-control.ps1'), '-Action', action, '-Root', str(self.root),
                '-BridgeTask', self.cfg['bridge_task'], '-OllamaTask', self.cfg['ollama_task']]
        for key, value in params.items():
            args += ['-' + key, str(value)]
        p = subprocess.run(args, capture_output=True, timeout=20 if action == 'Inspect' else 10,
                           creationflags=CREATE_NO_WINDOW, check=True)
        return json.loads(p.stdout.decode('utf-8-sig')) if p.stdout.strip() else {}

    def observe(self):
        runtime = self.command('Inspect')
        runtime['speakr'] = probe_http(self.cfg['speakr_health_url'])
        runtime['asr'] = probe_http(self.cfg['asr_health_url'])
        runtime['ollama'] = probe_llm(self.cfg)
        try:
            p = subprocess.run([self.cfg['docker'], 'info', '--format', '{{.ServerVersion}}'],
                               capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW)
            runtime['docker_ready'] = p.returncode == 0
        except subprocess.TimeoutExpired:
            runtime['docker_ready'] = False
        return runtime

    def action(self, component, verb, heartbeat=None):
        if self.cfg.get('supervision_candidate') and component in ('docker', 'containers'):
            raise RuntimeError('Candidate cannot control Docker or live containers')
        if component == 'docker':
            subprocess.Popen([self.cfg['docker_desktop']], creationflags=CREATE_NO_WINDOW,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif component == 'containers':
            import hashlib
            manifest = self.cfg.get('runtime_manifest', {})
            if hashlib.sha256(Path(self.cfg['compose_file']).read_bytes()).hexdigest() != manifest.get('compose_sha256'):
                raise RuntimeError('Compose drift; containers not changed')
            for name, expected in manifest.get('containers', {}).items():
                p = subprocess.run([self.cfg['docker'], 'inspect', '--format', '{{.Image}} {{.State.Running}}', name],
                                   capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW)
                values = p.stdout.decode().strip().split()
                if p.returncode == 0:
                    if not values or values[0] != expected['image_id']:
                        raise RuntimeError('Container image drift')
                    if values[1] == 'true':
                        continue
                    args = [self.cfg['docker'], 'start', name]
                else:
                    image = subprocess.run([self.cfg['docker'], 'image', 'inspect', '--format', '{{.Id}}', expected['image_ref']],
                                           capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW, check=True)
                    if image.stdout.decode().strip() != expected['image_id']:
                        raise RuntimeError('Local image drift; missing container not recreated')
                    args = [self.cfg['docker'], 'compose', '-f', self.cfg['compose_file'], 'up', '-d', '--no-recreate',
                            '--no-deps', '--no-build', '--pull', 'never', expected['service']]
                subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return  # Only the stopped/missing service; never touch the running peer.
            raise RuntimeError('Container manifest unavailable or differs')
        else:
            self.command(verb, Component=component,
                         **({'ProcessId': heartbeat['pid'], 'StartMarker': heartbeat['start_marker']} if heartbeat else {}))

    def notify(self, recovery=False):
        subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                          '-File', str(self.root / 'notify.ps1'), '-Event', 'Recovered' if recovery else 'Outage'],
                         creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Watchdog:
    def __init__(self, cfg, root=ROOT, runtime=None, clock=time.time):
        self.cfg, self.root, self.clock = cfg, Path(root), clock
        self.runtime = runtime or WindowsRuntime(cfg, root)
        self.state = read_json(self.root / 'watchdog-state.json', {})
        self.state.setdefault('started_at', clock())
        self.state.setdefault('components', {})

    def allowed(self, name):
        now = self.clock()
        c = self.state['components'].setdefault(name, {'actions': [], 'failures': 0})
        c['actions'] = [t for t in c['actions'] if now - t < 900]
        return len(c['actions']) < 3 and (not c['actions'] or now - c['actions'][-1] >= 120)

    def act(self, name, verb='Start', heartbeat=None):
        if getattr(self, 'acted_this_tick', False) or maintenance_paused(self.root) or not self.allowed(name):
            return False
        self.acted_this_tick = True
        # Persist the cooldown before spawning: a crash cannot produce an action storm.
        self.state['components'][name]['actions'].append(self.clock())
        atomic_json(self.root / 'watchdog-state.json', self.state)
        try:
            self.runtime.action(name, verb, heartbeat)
            return True
        except Exception as exc:
            self.state['components'][name]['error'] = type(exc).__name__
            return False

    def tick(self):
        now = self.clock()
        self.acted_this_tick = False
        if maintenance_paused(self.root):
            result = {'updated': now, 'maintenance': True, 'recovery_action': [], 'attention_required': False}
            atomic_json(self.root / 'watchdog-status.json', result)
            return result
        observed = self.runtime.observe()
        heartbeat = read_json(self.root / 'heartbeat.json', {})
        actions, problems = [], []
        for component in ('speakr', 'asr', 'ollama'):
            c = self.state['components'].setdefault(component, {'actions': [], 'failures': 0})
            c['failures'] = 0 if observed[component]['ready'] else c['failures'] + 1
            if not observed[component]['ready']:
                problems.append(component)
        bridge = observed.get('bridge', {})
        ollama = observed.get('ollama_task', {})
        alive = bridge.get('process_matches', False)
        fresh = heartbeat and now - heartbeat.get('updated', 0) < 45
        if not alive or not fresh:
            problems.append('bridge')
        if fresh and heartbeat.get('deadline') and now > heartbeat['deadline'] + 60:
            problems.append('bridge_operation_overdue')
        if not observed['docker_ready']:
            if self.act('docker'):
                actions.append('docker:start')
        elif observed.get('containers_absent', False):
            if self.act('containers'):
                actions.append('containers:start')
        # Running tasks with a dead endpoint are degraded, not safe to kill blindly.
        if not observed['ollama']['ready'] and ollama.get('enabled') and ollama.get('state') == 'Ready':
            if self.act('ollama'):
                actions.append('ollama:start')
        if bridge.get('enabled') and bridge.get('state') == 'Ready' and not alive:
            if self.act('bridge'):
                actions.append('bridge:start')
        if alive and not fresh and heartbeat.get('deadline') and now > heartbeat['deadline'] + 60:
            requested = self.state.get('bridge_stop_requested')
            if not requested:
                if not maintenance_paused(self.root):
                    atomic_json(self.root / 'stop.json', {'at': now})
                    self.state['bridge_stop_requested'] = now
                    actions.append('bridge:graceful-stop')
            elif now - requested >= 120 and bridge.get('enabled'):
                if self.act('bridge', 'StopVerified', heartbeat):
                    actions.append('bridge:stop-verified')
        if fresh:
            self.state.pop('bridge_stop_requested', None)
        if not problems:
            if self.state.get('notified'):
                self.runtime.notify(recovery=True)
            self.state.pop('outage_since', None)
            self.state['notified'] = False
        else:
            self.state.setdefault('outage_since', now)
            if now - self.state['outage_since'] >= 600 and not self.state.get('notified'):
                self.runtime.notify()
                self.state['notified'] = True
        result = {'updated': now, 'maintenance': False, 'health': observed,
                  'recovery_action': actions, 'attention_required': bool(problems),
                  'problems': problems, 'notification_emitted': bool(self.state.get('notified'))}
        atomic_json(self.root / 'watchdog-state.json', self.state)
        atomic_json(self.root / 'watchdog-status.json', result)
        return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config')
    args = p.parse_args()
    root = Path(args.config).resolve().parent if args.config else ROOT
    (root / 'watchdog').mkdir(exist_ok=True)
    try:
        with WorkerLock(root / 'watchdog'):
            Watchdog(config(args.config), root).tick()
    except RuntimeError as exc:
        if str(exc) != 'Bridge is already running':
            raise
    except Exception as exc:
        atomic_json(root / 'watchdog-status.json', {'updated': time.time(), 'attention_required': True,
                                                  'error': type(exc).__name__, 'recovery_action': []})
        raise


if __name__ == '__main__':
    main()
