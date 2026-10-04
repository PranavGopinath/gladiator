"""Optional, seeded SSH disruptions for disposable arena contestants."""
import random

INTERVAL = 30
WARNING = 10
RULES = ('\nARENA HAZARDS: Every 30 seconds, the referee randomly selects a living '
         'contestant and restores its starting SSH configuration and empty root password, '
         'restarting the SSH listener if needed. A warning names the target 10 seconds '
         'beforehand. You may defend again immediately; the hazard does not eliminate you. '
         'Read /tmp/arena-hazards.json on each turn for the latest warning or disruption.\n')

# Only listener processes get SIGHUP; existing SSH sessions are not killed.
RESET = '''import json, os, pathlib, signal, subprocess, sys
payload = json.load(sys.stdin)
pathlib.Path('/etc/ssh/sshd_config').write_text(payload['config'])
subprocess.run(['passwd', '-d', 'root'], check=True, stdout=subprocess.DEVNULL)
pathlib.Path('/run/sshd').mkdir(exist_ok=True)
subprocess.run(['ssh-keygen', '-A'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
listeners = []
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        exe = os.readlink(p / 'exe')
        args = (p / 'cmdline').read_bytes().replace(b'\\0', b' ')
        if exe == '/usr/sbin/sshd' and b'[listener]' in args:
            listeners.append(int(p.name))
    except OSError: pass
for pid in listeners:
    os.kill(pid, signal.SIGHUP)
if not listeners:
    subprocess.run(['/usr/sbin/sshd'], check=True)
'''
NOTICE = "import pathlib,sys; pathlib.Path('/tmp/arena-hazards.json').write_text(sys.stdin.read())"


class Hazards:
    def __init__(self, seed):
        self.random = random.Random(seed)
        self.next_at = INTERVAL
        self.target = None

    def tick(self, elapsed, alive):
        """Emit at most one pulse per call; never replay missed pulses in a burst."""
        alive = sorted(alive)
        if len(alive) < 2:
            return None
        if elapsed >= self.next_at:
            target = self.target
            at = self.next_at
            self.next_at = max(self.next_at + INTERVAL, (int(elapsed // INTERVAL) + 1) * INTERVAL)
            self.target = None
            if target not in alive:
                return {'action': 'skipped', 'target': target, 'at': at}
            return {'action': 'disruption', 'target': target, 'at': at}
        if self.target is None and elapsed >= self.next_at - WARNING:
            self.target = self.random.choice(alive)
            return {'action': 'warning', 'target': self.target, 'at': self.next_at}
        return None
