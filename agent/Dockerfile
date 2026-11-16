FROM node:22-bookworm-slim

# Everyday tools a coding agent reaches for. They are baked in because the
# running box has no internet to fetch them later.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates jq less procps make build-essential \
      python3 python3-pip python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*

# The coding agent itself, pinned so every student gets the same one.
ARG OPENCODE_VERSION=latest
RUN npm install -g opencode-ai@${OPENCODE_VERSION} && npm cache clean --force

# An ordinary user without sudo. The agent never runs as root.
RUN userdel -r node && useradd -m -u 1000 -s /bin/bash agent \
    && mkdir -p /workspace && chown agent:agent /workspace \
    && git config --system user.name "agent-box" \
    && git config --system user.email "agent@agent-box.invalid" \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory /workspace

COPY opencode.json AGENTS.md selftest.sh /opt/agent-box/
RUN chmod 755 /opt/agent-box/selftest.sh

ENV OPENCODE_CONFIG=/opt/agent-box/opencode.json \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:/home/agent/.local/bin:$PATH

USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
