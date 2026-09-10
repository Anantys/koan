-include .env
export

.PHONY: install onboard setup start stop status restart
.PHONY: clean say missions mission-rm migrate test test-skills test-strict coverage lint sync-instance rename-project release
.PHONY: awake run errand-run errand-awake dashboard koan api api-token openapi openapi-check webhook mcp mcp-setup mcp-config
.PHONY: ollama logs ssh-forward
.PHONY: install-systemctl-service uninstall-systemctl-service
.PHONY: install-user-service uninstall-user-service
.PHONY: install-launchd-service uninstall-launchd-service
.PHONY: docker-setup docker-pull-up docker-up docker-down docker-logs docker-test docker-auth docker-gh-auth

.DEFAULT_GOAL := koan

PYTHON_BIN ?= python3

VENV   ?= .venv
PYTHON ?= $(VENV)/bin/$(PYTHON_BIN)
# Absolute, normalized path — avoids `koan/../.venv/...` warnings from CPython's
# site module when the interpreter is invoked after `cd koan`.
PYTHON_ABS := $(abspath $(PYTHON))

# Shared invocation prefix: enter koan/, set runtime env, run the venv Python by
# absolute path. Used by every target that drives the agent or a CLI tool.
KOAN_RUN      := cd koan && KOAN_ROOT=$(PWD) PYTHONPATH=. $(PYTHON_ABS)
KOAN_TEST_RUN := cd koan && KOAN_ROOT=/tmp/test-koan PYTHONPATH=. $(PYTHON_ABS)
# Onboarding runs with TTY forced so the intro screen pauses correctly even when
# invoked through make (which can make stdin.isatty() return False).
KOAN_ONBOARD  := cd koan && KOAN_ROOT=$(PWD) PYTHONPATH=. KOAN_ONBOARDING_FORCE_TTY=1 $(PYTHON_ABS)

# --- pytest-xdist worker count ---
# Auto-pick the worker count for `make test` based on the environment:
#   * CI / GitHub Actions  → all available cores (`-n auto`)
#   * Remote SSH session   → 2 workers (be polite on shared hosts)
#   * Local terminal       → all available cores (`-n auto`)
# Override anytime with `make test PYTEST_WORKERS=N` (use 0 to disable xdist).
ifneq ($(CI),)
  PYTEST_WORKERS ?= auto
else ifneq ($(GITHUB_ACTIONS),)
  PYTEST_WORKERS ?= auto
else ifneq ($(SSH_CONNECTION)$(SSH_CLIENT)$(SSH_TTY),)
  PYTEST_WORKERS ?= 2
else
  PYTEST_WORKERS ?= auto
endif

ifeq ($(PYTEST_WORKERS),0)
  PYTEST_XDIST_ARGS :=
else
  PYTEST_XDIST_ARGS := -n $(PYTEST_WORKERS) --dist loadfile
endif

# --- service manager detection ---
# Default: foreground processes via pid_manager (no service manager)
# Set KOAN_SERVICE_MANAGER=systemd | systemd-user | launchd in .env to opt in.
#   systemd      — system service in /etc/systemd/system (needs sudo)
#   systemd-user — per-user service in ~/.config/systemd/user (no sudo; uses linger)
#   launchd      — macOS LaunchAgents
IS_LINUX := $(shell [ "$$(uname -s)" = "Linux" ] && echo 1)
IS_MAC := $(shell [ "$$(uname -s)" = "Darwin" ] && echo 1)
ifeq ($(KOAN_SERVICE_MANAGER),systemd)
  USE_SYSTEMD := 1
  USE_SYSTEMD_USER :=
  USE_LAUNCHD :=
else ifeq ($(KOAN_SERVICE_MANAGER),systemd-user)
  USE_SYSTEMD :=
  USE_SYSTEMD_USER := 1
  USE_LAUNCHD :=
else ifeq ($(KOAN_SERVICE_MANAGER),launchd)
  USE_SYSTEMD :=
  USE_SYSTEMD_USER :=
  USE_LAUNCHD := 1
else
  USE_SYSTEMD :=
  USE_SYSTEMD_USER :=
  USE_LAUNCHD :=
endif
SERVICE_INSTALLED = $(shell [ -f /etc/systemd/system/koan.service ] && echo 1)
USER_SERVICE_INSTALLED = $(shell [ -f ~/.config/systemd/user/koan.service ] && echo 1)
LAUNCHD_INSTALLED = $(shell [ -f ~/Library/LaunchAgents/com.koan.run.plist ] && echo 1)
# Bus-safe `systemctl --user` prefix: works from a normal login AND via `sudo -niu`
# (where XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS are unset). Linger keeps /run/user/<uid> alive.
SYSTEMCTL_USER = XDG_RUNTIME_DIR="$${XDG_RUNTIME_DIR:-/run/user/$$(id -u)}" DBUS_SESSION_BUS_ADDRESS="$${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$$(id -u)/bus}" systemctl --user

setup: $(VENV)/.installed

$(VENV)/.installed: koan/requirements.txt
	$(PYTHON_BIN) -m venv $(VENV)
	$(VENV)/bin/pip install -r koan/requirements.txt
	@touch $@

mcp-setup: $(VENV)/.mcp-installed

$(VENV)/.mcp-installed: $(VENV)/.installed koan/requirements-mcp.txt
	$(VENV)/bin/pip install -r koan/requirements-mcp.txt
	@touch $@

awake: setup
	$(KOAN_RUN) app/awake.py

run: setup
	$(KOAN_RUN) app/run.py

say: setup
	@test -n "$(m)" || (echo "Usage: make say m=\"your message\"" && exit 1)
	@$(KOAN_RUN) -c "from app.awake import handle_message; handle_message('$(m)')"

missions: setup
	@$(KOAN_RUN) -m app.mission_ctl list $(state)

mission-rm: setup
	@test -n "$(sel)" || (echo "Usage: make mission-rm sel=i1  (or sel=p2, or a keyword)" && exit 1)
	@$(KOAN_RUN) -m app.mission_ctl delete "$(sel)"

lint: setup
	$(VENV)/bin/pip install -q ruff 2>/dev/null
	$(VENV)/bin/ruff check koan/

test: setup
	@echo "→ pytest workers: $(PYTEST_WORKERS)"
	$(VENV)/bin/pip install -q pytest pytest-cov pytest-xdist 2>/dev/null
	$(KOAN_TEST_RUN) -m pytest tests/ -v $(PYTEST_XDIST_ARGS) --cov=app --cov-report=term-missing --cov-report=html:htmlcov
	@$(MAKE) --no-print-directory test-skills

test-skills: setup
	@if [ -d instance/skills ] && find -L instance/skills -path '*/tests/test_*.py' -print -quit 2>/dev/null | grep -q .; then \
		$(VENV)/bin/pip install -q pytest pytest-cov pytest-xdist 2>/dev/null; \
		echo "→ running skill-local tests (instance/skills/**/tests)"; \
		KOAN_REPO=$(PWD) KOAN_ROOT=/tmp/test-koan PYTHONPATH=koan $(PYTHON) -m pytest instance/skills/ -v $(PYTEST_XDIST_ARGS); \
	else \
		echo "→ no skill-local tests found under instance/skills/**/tests/ — skipping"; \
	fi

test-strict: setup
	@echo "→ running full test suite in strict mode (0 failures required, workers: $(PYTEST_WORKERS))"
	$(VENV)/bin/pip install -q pytest pytest-cov pytest-xdist 2>/dev/null
	@$(KOAN_TEST_RUN) -m pytest tests/ -q --tb=short $(PYTEST_XDIST_ARGS) \
		|| (echo "✗ tests failed — aborting" && exit 1)
	@if [ -d instance/skills ] && find -L instance/skills -path '*/tests/test_*.py' -print -quit 2>/dev/null | grep -q .; then \
		KOAN_REPO=$(PWD) KOAN_ROOT=/tmp/test-koan PYTHONPATH=koan $(PYTHON) -m pytest instance/skills/ -q --tb=short $(PYTEST_XDIST_ARGS) \
			|| (echo "✗ skill-local tests failed — aborting" && exit 1); \
	fi
	@echo "✓ all tests passed"

release:
	@echo "✗ 'make release' is deprecated: it tagged directly from main, bypassing the incubating pipeline."
	@echo "  Use /koan.incubate to prep, then dispatch the Release workflow on 'incubating':"
	@echo "    gh workflow run release.yml --ref incubating -f version=vX.Y.Z"
	@echo "  See docs/operations/maint.md."
	@exit 1

migrate: setup
	$(KOAN_RUN) app/migrate_memory.py

dashboard: setup
	$(KOAN_RUN) app/dashboard/__main__.py $(if $(KOAN_DASHBOARD_HOST),--host $(KOAN_DASHBOARD_HOST),) $(if $(KOAN_DASHBOARD_PORT),--port $(KOAN_DASHBOARD_PORT),)

koan: setup
	@$(KOAN_RUN) -m app.koan_cli $(PWD)

api: setup
	$(KOAN_RUN) app/api/server.py $(if $(KOAN_API_HOST),--host $(KOAN_API_HOST),) $(if $(KOAN_API_PORT),--port $(KOAN_API_PORT),)

api-token:
	@token=$$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") && \
		echo "Generated API token:" && \
		echo "" && \
		echo "  $$token" && \
		echo "" && \
		echo "Add to your .env file:" && \
		echo "  echo 'KOAN_API_TOKEN=$$token' >> .env" && \
		echo "" && \
		echo "Or set in instance/config.yaml:" && \
		echo "  api:" && \
		echo "    token: \"$$token\""

mcp: mcp-setup
	$(KOAN_RUN) -m app.mcp

mcp-config: setup
	@$(KOAN_RUN) -m app.mcp.config_cli

# Regenerate the committed OpenAPI document (koan/openapi.yaml) from the live
# REST API route table. Run this after adding/removing/modifying an API endpoint,
# then commit koan/openapi.yaml in the same change.
openapi: setup
	$(KOAN_RUN) -m app.api.openapi_gen --output openapi.yaml

# Fail if koan/openapi.yaml has drifted from the REST API code (what CI runs).
openapi-check: setup
	$(KOAN_RUN) -m app.api.openapi_gen --check --output openapi.yaml

# Standalone GitHub webhook receiver (alternative to the bridge-embedded one).
# Requires KOAN_GITHUB_WEBHOOK_SECRET. Front with a tunnel (smee/cloudflared).
webhook: setup
	cd koan && KOAN_ROOT=$(PWD) PYTHONPATH=. ../$(PYTHON) -m app.github_webhook

restart:
	$(MAKE) stop
	@sleep 1
	$(MAKE) start

ifeq ($(USE_SYSTEMD),1)

start: setup
	@if [ -n "$$SSH_AUTH_SOCK" ]; then \
		ln -sf "$$SSH_AUTH_SOCK" "$(PWD)/.ssh-agent-sock"; \
		echo "✓ SSH agent socket forwarded"; \
	fi
	@if [ -z "$(SERVICE_INSTALLED)" ]; then \
		echo "→ systemd detected — installing Kōan service (one-time setup)..."; \
		sudo CALLER_PATH="$$PATH" bash koan/systemd/install-service.sh "$(PWD)" "$(PWD)/$(PYTHON)"; \
	fi
	@sudo systemctl start koan

stop:
	@sudo systemctl stop koan koan-awake

status:
	@sudo systemctl status koan koan-awake --no-pager || true

else ifeq ($(USE_SYSTEMD_USER),1)

start: setup
	@if [ -n "$$SSH_AUTH_SOCK" ]; then \
		ln -sf "$$SSH_AUTH_SOCK" "$(PWD)/.ssh-agent-sock"; \
		echo "✓ SSH agent socket forwarded"; \
	fi
	@if [ -z "$(USER_SERVICE_INSTALLED)" ]; then \
		echo "→ systemd (user) detected — installing Kōan user service (one-time setup)..."; \
		bash koan/systemd/install-user-service.sh "$(PWD)" "$(PWD)/$(PYTHON)"; \
	fi
	@$(SYSTEMCTL_USER) start koan.service
	@echo "✓ Kōan started via systemd --user"

stop:
	@$(SYSTEMCTL_USER) stop koan.service koan-awake.service 2>/dev/null || true

status:
	@$(SYSTEMCTL_USER) status koan.service koan-awake.service --no-pager || true

else ifeq ($(USE_LAUNCHD),1)

start: setup
	@if [ -n "$$SSH_AUTH_SOCK" ]; then \
		ln -sf "$$SSH_AUTH_SOCK" "$(PWD)/.ssh-agent-sock"; \
		echo "✓ SSH agent socket forwarded"; \
	fi
	@if [ -z "$(LAUNCHD_INSTALLED)" ]; then \
		echo "→ launchd detected — installing Kōan service (one-time setup)..."; \
		bash koan/launchd/install-service.sh "$(PWD)"; \
	fi
	@launchctl bootstrap "gui/$$(id -u)" ~/Library/LaunchAgents/com.koan.awake.plist 2>/dev/null || true
	@launchctl bootstrap "gui/$$(id -u)" ~/Library/LaunchAgents/com.koan.run.plist 2>/dev/null || true
	@if [ -f ~/Library/LaunchAgents/com.koan.dashboard.plist ]; then \
		launchctl bootstrap "gui/$$(id -u)" ~/Library/LaunchAgents/com.koan.dashboard.plist 2>/dev/null || true; \
	fi
	@$(KOAN_RUN) -c "from pathlib import Path; from app.pid_manager import _show_startup_banner; from app.utils import get_cli_provider_env; _show_startup_banner(Path('$(PWD)'), get_cli_provider_env())"
	@echo "✓ Kōan started via launchd"

stop:
	@launchctl bootout "gui/$$(id -u)/com.koan.run" 2>/dev/null || true
	@launchctl bootout "gui/$$(id -u)/com.koan.awake" 2>/dev/null || true
	@launchctl bootout "gui/$$(id -u)/com.koan.dashboard" 2>/dev/null || true
	@echo "✓ Kōan stopped"

status:
	@echo "=== com.koan.run ===" && launchctl print "gui/$$(id -u)/com.koan.run" 2>/dev/null | head -20 || echo "  not loaded"
	@echo "=== com.koan.awake ===" && launchctl print "gui/$$(id -u)/com.koan.awake" 2>/dev/null | head -20 || echo "  not loaded"

else

start: setup
	@$(KOAN_RUN) -m app.pid_manager start-all $(PWD)

stop: setup
	@if [ "$$(uname -s)" = "Darwin" ] && launchctl list com.koan.run >/dev/null 2>&1; then \
		echo "⚠ Launchd services detected — running bootout..."; \
		launchctl bootout "gui/$$(id -u)/com.koan.run" 2>/dev/null || true; \
		launchctl bootout "gui/$$(id -u)/com.koan.awake" 2>/dev/null || true; \
	fi
	@$(KOAN_RUN) -m app.pid_manager stop-all $(PWD)

status: setup
	@$(KOAN_RUN) -m app.pid_manager status-all $(PWD)

endif

ssh-forward:
	@if [ -n "$$SSH_AUTH_SOCK" ]; then \
		ln -sf "$$SSH_AUTH_SOCK" "$(PWD)/.ssh-agent-sock"; \
		echo "✓ SSH agent socket forwarded to .ssh-agent-sock"; \
	else \
		echo "⚠ No SSH agent detected (SSH_AUTH_SOCK not set)"; \
	fi

errand-run: setup
	caffeinate -i $(MAKE) run

errand-awake: setup
	caffeinate -i sh -c '$(KOAN_RUN) app/awake.py'

ollama: setup
	@echo "→ Starting Kōan with Ollama stack..."
	@$(KOAN_RUN) -m app.pid_manager start-stack $(PWD)

logs:
	@mkdir -p logs
	@if [ ! -f logs/run.log ] && [ ! -f logs/awake.log ] && [ ! -f logs/ollama.log ]; then \
		echo "No log files found. Start Kōan first with 'make start'."; \
		exit 1; \
	fi
	@echo "→ Watching Kōan logs + live progress (Ctrl-C to stop watching — Kōan keeps running)"
	@if [ "$(raw)" = "1" ]; then \
		tail -F logs/run.log logs/awake.log logs/ollama.log instance/journal/pending.md 2>/dev/null; \
	else \
		fmt_py="$(PYTHON_ABS)"; \
		[ -x "$$fmt_py" ] || fmt_py="$$(command -v python3 || command -v python)"; \
		if [ -z "$$fmt_py" ]; then \
			tail -F logs/run.log logs/awake.log logs/ollama.log instance/journal/pending.md 2>/dev/null; \
		else \
			tail -F logs/run.log logs/awake.log logs/ollama.log instance/journal/pending.md 2>/dev/null \
				| ( cd koan && PYTHONPATH=. "$$fmt_py" -m app.log_fmt ); \
		fi; \
	fi

install:
	@$(MAKE) --no-print-directory setup
	@$(KOAN_ONBOARD) -m app.onboarding

onboard: setup
	@$(KOAN_ONBOARD) -m app.onboarding $(ARGS)

rename-project: setup
	@test -n "$(old)" || (echo "Usage: make rename-project old=foo new=bar [apply=1]" && exit 1)
	@test -n "$(new)" || (echo "Usage: make rename-project old=foo new=bar [apply=1]" && exit 1)
	$(KOAN_RUN) -m app.rename_project $(old) $(new) $(if $(apply),--apply,)

clean:
	rm -rf $(VENV)

sync-instance:
	@mkdir -p instance
	@for f in instance.example/*; do \
		name=$$(basename "$$f"); \
		if [ ! -e "instance/$$name" ]; then \
			echo "→ Copying $$name"; \
			cp -r "$$f" "instance/$$name"; \
		fi; \
	done
	@echo "✓ instance/ synced with instance.example/"

install-systemctl-service: setup
	@if [ -z "$(IS_LINUX)" ]; then echo "Error: systemd is only available on Linux." >&2; exit 1; fi
	@if [ -z "$(USE_SYSTEMD)" ]; then echo "Error: systemctl not found. systemd is required." >&2; exit 1; fi
	sudo CALLER_PATH="$$PATH" bash koan/systemd/install-service.sh "$(PWD)" "$(PWD)/$(PYTHON)"

uninstall-systemctl-service:
	@-$(MAKE) stop
	@if [ -z "$(IS_LINUX)" ]; then echo "Error: systemd is only available on Linux." >&2; exit 1; fi
	@if [ -z "$(USE_SYSTEMD)" ]; then echo "Error: systemctl not found." >&2; exit 1; fi
	sudo bash koan/systemd/uninstall-service.sh

install-user-service: setup
	@if [ -z "$(IS_LINUX)" ]; then echo "Error: systemd is only available on Linux." >&2; exit 1; fi
	@command -v systemctl >/dev/null 2>&1 || (echo "Error: systemctl not found. systemd is required." >&2; exit 1)
	bash koan/systemd/install-user-service.sh "$(PWD)" "$(PWD)/$(PYTHON)"

uninstall-user-service:
	@if [ -z "$(IS_LINUX)" ]; then echo "Error: systemd is only available on Linux." >&2; exit 1; fi
	@command -v systemctl >/dev/null 2>&1 || (echo "Error: systemctl not found." >&2; exit 1)
	bash koan/systemd/uninstall-user-service.sh $(if $(disable-linger),--disable-linger,)

install-launchd-service: setup
	@if [ -z "$(IS_MAC)" ]; then echo "Error: launchd is only available on macOS." >&2; exit 1; fi
	@if [ -z "$(HAS_LAUNCHCTL)" ]; then echo "Error: launchctl not found." >&2; exit 1; fi
	bash koan/launchd/install-service.sh "$(PWD)"

uninstall-launchd-service:
	@-$(MAKE) stop
	@if [ -z "$(IS_MAC)" ]; then echo "Error: launchd is only available on macOS." >&2; exit 1; fi
	@if [ -z "$(HAS_LAUNCHCTL)" ]; then echo "Error: launchctl not found." >&2; exit 1; fi
	bash koan/launchd/uninstall-service.sh

# --- Docker targets ---

# Published image on GitHub Container Registry. Override the tag to pin a
# release, e.g. make docker-pull-up KOAN_IMAGE=ghcr.io/anantys-oss/koan:stable
KOAN_IMAGE ?= ghcr.io/anantys-oss/koan:latest

docker-setup:
	@./setup-docker.sh

# Recommended: pull the prebuilt image and start (no local build).
docker-pull-up: docker-setup
	KOAN_IMAGE="$(KOAN_IMAGE)" docker compose pull
	KOAN_IMAGE="$(KOAN_IMAGE)" docker compose up -d --no-build
	@echo "→ Kōan running from prebuilt image $(KOAN_IMAGE). Use 'make docker-logs' to watch output."

# Fallback: build the image from source and start.
docker-up: docker-setup
	docker compose up --build -d
	@echo "→ Kōan running in Docker. Use 'make docker-logs' to watch output."

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-test:
	docker compose run --rm koan test

docker-auth:
	@command -v claude >/dev/null 2>&1 || { echo "Error: Claude CLI not found on host."; echo "Install: https://docs.anthropic.com/en/docs/claude-code/overview"; exit 1; }
	@echo "Running 'claude setup-token' to generate a long-lived OAuth token..."
	@echo "(Complete the flow in your browser if prompted)"
	@echo ""
	@tmpfile=$$(mktemp /tmp/koan-auth.XXXXXX) && \
		(script -q "$$tmpfile" claude setup-token || true) && \
		echo "" && \
		echo "Extracting token from output..." && \
		token=$$(perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g; s/\r//g' "$$tmpfile" | grep -oE 'sk-ant-[A-Za-z0-9_-]+' | head -1) && \
		rm -f "$$tmpfile" && \
		if [ -z "$$token" ]; then echo "Error: Could not extract token. Run 'claude auth login' first."; exit 1; fi && \
		touch .env && \
		if grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' .env 2>/dev/null; then \
			sed -i.bak 's|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN='"$$token"'|' .env && rm -f .env.bak; \
		else \
			echo "CLAUDE_CODE_OAUTH_TOKEN=$$token" >> .env; \
		fi && \
		echo "✓ Token saved to .env — container will use it on next start."

docker-gh-auth:
	@command -v gh >/dev/null 2>&1 || { echo "Error: GitHub CLI (gh) not found on host."; echo "Install: https://cli.github.com"; exit 1; }
	@echo "Extracting GitHub token from host..."
	@token=$$(gh auth token 2>/dev/null || true) && \
		if [ -z "$$token" ]; then echo "Error: No GitHub token found. Run 'gh auth login' first."; exit 1; fi && \
		touch .env && \
		if grep -q '^GH_TOKEN=' .env 2>/dev/null; then \
			sed -i.bak 's|^GH_TOKEN=.*|GH_TOKEN='"$$token"'|' .env && rm -f .env.bak; \
		else \
			echo "GH_TOKEN=$$token" >> .env; \
		fi && \
		echo "✓ GitHub token saved to .env — container will use it on next start."
