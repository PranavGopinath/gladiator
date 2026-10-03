"""One permanent contestant process, with multiple resumable model turns."""
import json
import os
import re
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


def policy_block(value):
    """Recognize explicit provider policy errors, never ordinary tool output."""
    text = json.dumps(value) if not isinstance(value, str) else value
    return bool(re.search(r'flagged for possible cybersecurity risk|content[_ -]filter|'
                          r'policy[_ -](?:violation|block|denial)|safety[_ -](?:block|violation)|'
                          r'blocked.{0,40}(?:policy|safety)|guardrail|safeguards stopped', text, re.I))


def park(interval, max_turns=0):
    # A blocked contestant stays alive without repeatedly submitting or changing
    # the blocked request. Finite-turn mode is reserved for fixture tests.
    if max_turns:
        return 0
    while True:
        time.sleep(max(interval, 0.1))


def run(initial, kind, interval=15, max_turns=0):
    session_id = None
    turn = 0
    session_started = time.monotonic()
    env = dict(os.environ, ARENA_SESSION_PID=str(os.getpid()))
    print(json.dumps({'type': 'arena.model', 'harness': kind,
                      'model': os.environ.get('MODEL', '')}), flush=True)
    while True:
        turn += 1
        prompt = (initial[-1] + f"\nYour persistent contestant session PID is {os.getpid()}. "
                  "Normal replies do not end this session; a new observation turn follows automatically."
                  if not session_id else f"Turn {turn}; approximately {int(time.monotonic() - session_started)} seconds elapsed. " + FOLLOWUP)
        started = time.time()
        emit('active', turn, started_at=started, session_id=session_id)
        try:
            child = subprocess.Popen(turn_command(initial, kind, session_id, prompt), env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        except OSError:
            emit('failed', turn, error_category='startup', exit_code=1,
                 session_id=session_id, message='Cannot launch model process.')
            return 1
        failed = False
        blocked = False
        conversation_mismatch = False
        try:
            for line in child.stdout:
                print(line, end='', flush=True)
                try:
                    data = json.loads(line)
                except ValueError:
                    blocked = blocked or policy_block(line)
                    continue
                if not isinstance(data, dict):
                    continue
                reported_id = None
                if data.get('type') == 'thread.started':
                    reported_id = data.get('thread_id')
                if data.get('type') == 'system' and data.get('subtype') == 'init':
                    reported_id = data.get('session_id')
                if reported_id:
                    if session_id and reported_id != session_id:
                        conversation_mismatch = True
                    else:
                        session_id = reported_id
                if data.get('type') == 'turn.failed' or (data.get('type') == 'result' and data.get('is_error')):
                    failed = True
                    blocked = blocked or policy_block(data.get('error') or data.get('errors') or data.get('result') or data.get('message') or '')
                if data.get('type') == 'error':
                    blocked = blocked or policy_block(data.get('error') or data.get('message') or '')
        finally:
            child.stdout.close()
        code = child.wait()
        if code < 0:
            emit('failed', turn, exit_code=code, duration_seconds=time.time() - started,
                 session_id=session_id, error_category='model_signal',
                 message='Model process terminated by signal; contestant will not restart.')
            return 128 - code
        if blocked:
            emit('blocked', turn, exit_code=code, duration_seconds=time.time() - started,
                 session_id=session_id, error_category='policy_block', next_turn_at=None,
                 message='Provider policy blocked the turn. Session remains alive; further model calls are parked.')
            return park(interval, max_turns)
        if code or failed or not session_id or conversation_mismatch:
            category = ('conversation_mismatch' if conversation_mismatch else 'cli_exit' if code or failed else 'conversation_missing')
            emit('turn_error', turn, exit_code=code, duration_seconds=time.time() - started,
                 session_id=session_id, error_category=category, next_turn_at=time.time() + interval,
                 message='Model turn could not complete. Session remains alive; next scheduled turn retains the conversation.')
            if max_turns and turn >= max_turns:
                return 0
            time.sleep(interval)
            continue
        emit('idle', turn, session_id=session_id, duration_seconds=time.time() - started,
             next_turn_at=time.time() + interval,
             message=f'Turn complete. Session remains alive; next observation in {interval:g}s.')
        if max_turns and turn >= max_turns:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    kind = os.environ['AGENT']
    interval = float(os.environ.get('TURN_INTERVAL_SECONDS', '15'))
    max_turns = int(os.environ.get('SESSION_TEST_TURNS', '0'))
    if kind in ('gemini', 'grok', 'compatible'):
        from universal_harness import run_session
        sys.exit(run_session(agent_command()[-1], interval, max_turns))
    sys.exit(run(agent_command(), kind, interval, max_turns))
