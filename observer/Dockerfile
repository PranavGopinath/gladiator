FROM debian:bookworm-slim

# Installed at image build time; match startup never installs packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bpftrace python3 procps util-linux && rm -rf /var/lib/apt/lists/*
COPY identity.py /opt/observer/identity.py
ENTRYPOINT ["sh", "-c", "mount -t tracefs tracefs /sys/kernel/tracing && exec sleep infinity"]
