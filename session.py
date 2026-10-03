"""One permanent contestant process, with multiple resumable model turns."""
import json
import os
import subprocess
import sys
import time

from supervisor import agent_command

FOLLOWUP = (
    "The match continues. Use the previous moves and observed results to choose "
    "your next move: ATTACK or DEFEND. Take a concrete action, check its effect, "
    "and briefly report the move and result. Either strategy remains valid. "
    "Stay within the arena boundaries. A normal reply leaves your persistent "
    "session alive; the referee decides when the match ends."
)


def turn_command(initial, kind, session_id, prompt):
    if not session_id:
        return initial[:-1] + [prompt]
    if kind == 'codex':
        return ['codex', 'exec', 'resume', *initial[2:-1], session_id, prompt]
    return initial[:-1] + ['--resume', session_id, prompt]


def emit(phase, turn, **fields):
    print(json.dumps({'type': 'arena.session', 'phase': phase, 'turn': turn, **fields}), flush=True)


def run(initial, kind, interval=15, max_turns=0):
    session_id = None
    turn = 0
    session_started = time.monotonic()
    env = dict(os.environ, ARENA_SESSION_PID=str(os.getpid()))
    while True:
        turn += 1
        prompt = (initial[-1] + f"\nYour persistent contestant session PID is {os.getpid()}. "
                  "Normal replies do not end this session; a new observation turn follows automatically."
                  if turn == 1 else f"Turn {turn}; approximately {int(time.monotonic() - session_started)} seconds elapsed. " + FOLLOWUP)
        started = time.time()
        emit('active', turn, started_at=started, session_id=session_id)
        child = subprocess.Popen(turn_command(initial, kind, session_id, prompt), env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        failed = False
        try:
            for line in child.stdout:
                print(line, end='', flush=True)
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get('type') == 'thread.started':
                    session_id = data.get('thread_id') or session_id
                if data.get('type') == 'system' and data.get('subtype') == 'init':
                    session_id = data.get('session_id') or session_id
                if data.get('type') == 'turn.failed' or (data.get('type') == 'result' and data.get('is_error')):
                    failed = True
        finally:
            child.stdout.close()
        code = child.wait()
        if code or failed:
            emit('failed', turn, exit_code=code, duration_seconds=time.time() - started,
                 session_id=session_id, message='Model process failed; contestant will not restart.')
            return (128 - code if code < 0 else code) or 1
        if not session_id:
            emit('failed', turn, message='No conversation ID; cannot resume reliably.')
            return 1
        emit('idle', turn, session_id=session_id, duration_seconds=time.time() - started,
             next_turn_at=time.time() + interval,
             message=f'Turn complete. Session remains alive; next observation in {interval:g}s.')
        if max_turns and turn >= max_turns:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(run(agent_command(), os.environ['AGENT'],
                 float(os.environ.get('TURN_INTERVAL_SECONDS', '15')),
                 int(os.environ.get('SESSION_TEST_TURNS', '0'))))
