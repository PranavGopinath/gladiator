import hashlib
import os
import signal
import subprocess
import time
from pathlib import Path

process = None
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
def digest():
    try:
        return hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path('/app').glob('*.py')))).hexdigest()
    except OSError:
        return ''
last = None
while not stopping:
    current = digest()
    if current != last or process is None or process.poll() is not None:
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        process = subprocess.Popen(['python3', '-u', '/app/server.py'], cwd='/app')
        last = current
    time.sleep(0.5)
if process and process.poll() is None:
    process.terminate()
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired: process.kill()
