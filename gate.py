"""Keep the original PID while waiting for the host's common start time."""
import json
import os
from pathlib import Path
import sys
import time

gate = Path('/tmp/arena-start.json')
while not gate.exists():
    time.sleep(0.02)
start = json.loads(gate.read_text())['start_at']
while time.time() < start:
    time.sleep(max(0, min(0.02, start - time.time())))
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
