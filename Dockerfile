FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.160.0
ARG CLAUDE_VERSION=2.1.288
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv git curl ca-certificates procps iproute2 bash sudo openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} \
    && npm cache clean --force
RUN echo 'node ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/arena-node \
    && chmod 0440 /etc/sudoers.d/arena-node \
    && passwd -d root \
    && rm -f /etc/ssh/ssh_host_*
COPY sshd_config /etc/ssh/sshd_config
WORKDIR /workspace
RUN chown node:node /workspace
COPY supervisor.py demo.py task.txt entrypoint.sh gate.py session.py universal_harness.py /opt/arena/
EXPOSE 22 8080 8000
CMD ["bash", "/opt/arena/entrypoint.sh"]
