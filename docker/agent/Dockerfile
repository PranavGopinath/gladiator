FROM node:20-bookworm-slim
ARG CLAUDE_VERSION=2.1.288
ARG CODEX_VERSION=0.156.1
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl git ca-certificates procps && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} @openai/codex@${CODEX_VERSION}
RUN mkdir /app && chown node:node /app
USER 1000:1000
WORKDIR /app
CMD ["sleep", "infinity"]
