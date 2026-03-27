# Kōan Docker Image
#
# Runtime tools (git, gh, node, Claude CLI) are all installed in the image.
# Auth: ANTHROPIC_API_KEY in .env (API billing) or interactive login (subscription).
# GitHub CLI auth (~/.config/gh) is mounted from the host.
#
# Build:  docker build -t koan .
# Run:    docker compose up --build
# Setup:  ./setup-docker.sh  (auto-detects host paths, generates mounts)

FROM python:3.12-slim

# System dependencies + Node.js (for Claude CLI) + gh (GitHub CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    jq \
    curl \
    bash \
    procps \
    openssh-client \
    make \
    nodejs \
    npm \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI via npm (can't mount host binary across architectures)
RUN npm install -g @anthropic-ai/claude-code

# Configurable UID/GID — match the host user to avoid permission issues
# on bind-mounted volumes (workspace, ~/.config/gh, etc.)
ARG HOST_UID=1000
ARG HOST_GID=1000

# Create group — if a group with HOST_GID already exists (e.g. macOS GID 20
# maps to dialout in Debian), reuse it; otherwise create "koan" group.
# Then create the koan user with the desired UID, assigned to that GID.
RUN if getent group ${HOST_GID} >/dev/null 2>&1; then \
        echo "GID ${HOST_GID} already exists — reusing"; \
    else \
        groupadd -g ${HOST_GID} koan; \
    fi \
    && useradd -u ${HOST_UID} -g ${HOST_GID} -m -s /bin/bash koan 2>/dev/null || \
       useradd -u ${HOST_UID} -g ${HOST_GID} -M -s /bin/bash koan 2>/dev/null || true

# App directory
WORKDIR /app

# Python dependencies (cached layer — changes rarely)
COPY koan/requirements.txt /app/koan/requirements.txt
RUN pip install --no-cache-dir -r /app/koan/requirements.txt \
    && pip install --no-cache-dir pytest supervisor

# Copy application code
COPY koan/ /app/koan/
COPY instance.example/ /app/instance.example/
COPY Makefile /app/
COPY CLAUDE.md /app/
COPY docs/ /app/docs/
COPY projects.example.yaml /app/

# Supervisor config + restart-delay wrapper
COPY koan/docker/supervisord.conf /etc/supervisord.conf
COPY koan/docker/supervised-run.sh /app/koan/docker/supervised-run.sh
RUN chmod +x /app/koan/docker/supervised-run.sh

# Entrypoint
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Workspace + runtime directories
RUN mkdir -p /app/workspace /app/instance /app/logs /home/koan/.claude \
    && echo '{"hasCompletedOnboarding": true}' > /home/koan/.claude.json \
    && chown -R ${HOST_UID}:${HOST_GID} /app /home/koan/.claude /home/koan/.claude.json

# Switch to non-root user
USER ${HOST_UID}

# Git config for the koan user (can be overridden by mounting ~/.gitconfig)
RUN git config --global user.name "Kōan" \
    && git config --global user.email "koan@noreply.github.com" \
    && git config --global init.defaultBranch main

# Health check: verify heartbeat file is fresh (< 120s old)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD test -f /app/.koan-heartbeat && \
        [ $(( $(date +%s) - $(cat /app/.koan-heartbeat | cut -d. -f1) )) -lt 120 ]

ENV KOAN_ROOT=/app
ENV PYTHONPATH=/app/koan
# Force Node.js to resolve localhost to IPv4 first — avoids IPv6 binding
# issues in some Docker setups. See: anthropics/claude-code#9376
ENV NODE_OPTIONS="--dns-result-order=ipv4first"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["start"]
