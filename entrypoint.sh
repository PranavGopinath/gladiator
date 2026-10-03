#!/bin/bash
set -euo pipefail
mkdir -p /run/sshd
# Each computer gets unique server keys; no shared private host keys in the image.
ssh-keygen -A >/dev/null
/usr/sbin/sshd -t
/usr/sbin/sshd
exec python3 -u /opt/arena/supervisor.py
