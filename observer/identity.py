"""Read Linux VM process identities from the observer's host PID namespace."""
import json
import os
from pathlib import Path
import sys


def identity(pid):
    pid = int(pid)
    proc = Path('/proc') / str(pid)
    # comm may contain spaces and parentheses; fields start after its final ')'.
    stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(stat[19])  # field 22, with state (field 3) at index zero
    hz = os.sysconf('SC_CLK_TCK')
    namespace = int(os.readlink(proc / 'ns/pid').split('[')[1].rstrip(']'))
    cgroup_path = (proc / 'cgroup').read_text().strip()
    return dict(pid=pid, tracked_pid=pid, namespace=namespace, pidns=namespace,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                start_ticks=ticks, clock_ticks=hz,
                start_min_ns=(ticks * 1_000_000_000 + hz - 1) // hz,
                start_max_ns=((ticks + 1) * 1_000_000_000 + hz - 1) // hz,
                cgroup_path=cgroup_path)


if __name__ == '__main__':
    print(json.dumps({arg: identity(arg) for arg in sys.argv[1:]}))
