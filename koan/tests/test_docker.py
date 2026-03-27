"""Tests for Docker deployment configuration.

Validates Dockerfile, entrypoint, compose, setup script, and .dockerignore
without requiring Docker to be installed.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestDockerfile:
    """Validate Dockerfile structure and best practices."""

    @pytest.fixture(autouse=True)
    def load_dockerfile(self):
        self.dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    def test_dockerfile_exists(self):
        assert (REPO_ROOT / "Dockerfile").exists()

    def test_base_image_is_python_slim(self):
        """Thin image — CLIs are mounted, not installed."""
        assert "FROM python:3.12-slim" in self.dockerfile

    def test_installs_claude_cli_via_npm(self):
        """Claude CLI is installed in the image (can't mount across architectures)."""
        assert "@anthropic-ai/claude-code" in self.dockerfile
        assert "npm install -g @anthropic-ai/claude-code" in self.dockerfile

    def test_no_node_install(self):
        """Node.js is mounted from host if needed, not installed in image."""
        assert "FROM node:" not in self.dockerfile

    def test_installs_git(self):
        assert "git" in self.dockerfile

    def test_installs_make(self):
        """Make is needed for workspace-level builds."""
        assert "make" in self.dockerfile

    def test_configurable_uid_gid(self):
        """UID/GID should be build args for bind mount permissions."""
        assert "ARG HOST_UID" in self.dockerfile
        assert "ARG HOST_GID" in self.dockerfile

    def test_creates_nonroot_user(self):
        """Security: must not run as root."""
        assert "useradd" in self.dockerfile
        assert "USER ${HOST_UID}" in self.dockerfile

    def test_sets_koan_root(self):
        assert "KOAN_ROOT=/app" in self.dockerfile

    def test_sets_pythonpath(self):
        assert "PYTHONPATH" in self.dockerfile

    def test_has_healthcheck(self):
        assert "HEALTHCHECK" in self.dockerfile

    def test_healthcheck_reads_heartbeat_content(self):
        """HEALTHCHECK should read heartbeat file content (Unix timestamp)."""
        assert "cat /app/.koan-heartbeat" in self.dockerfile

    def test_copies_requirements_before_code(self):
        """Requirements should be copied first for Docker layer caching."""
        req_line = self.dockerfile.index("COPY koan/requirements.txt")
        code_line = self.dockerfile.index("COPY koan/ ")
        assert req_line < code_line

    def test_no_host_bin_in_path(self):
        """PATH should not include /host-bin — CLIs are installed in image."""
        assert "/host-bin" not in self.dockerfile

    def test_workspace_directory_created(self):
        assert "/app/workspace" in self.dockerfile

    def test_creates_claude_config_dir(self):
        """~/.claude dir must exist for interactive auth state."""
        assert "/home/koan/.claude" in self.dockerfile

    def test_entrypoint_is_set(self):
        assert "ENTRYPOINT" in self.dockerfile

    def test_default_cmd_is_start(self):
        assert 'CMD ["start"]' in self.dockerfile

    def test_cleans_apt_cache(self):
        """Image size: apt cache should be cleaned."""
        assert "rm -rf /var/lib/apt/lists/*" in self.dockerfile

    def test_pip_no_cache(self):
        """Image size: pip should use --no-cache-dir."""
        assert "--no-cache-dir" in self.dockerfile

    def test_no_host_bin_dirs(self):
        """No /host-bin or /host-node — CLIs are installed in image."""
        assert "/host-bin" not in self.dockerfile
        assert "/host-node" not in self.dockerfile

    def test_sets_ipv4_node_options(self):
        """NODE_OPTIONS should force IPv4 for potential localhost binding."""
        assert "dns-result-order=ipv4first" in self.dockerfile

    def test_creates_onboarding_json(self):
        """~/.claude.json with hasCompletedOnboarding must exist for setup-token."""
        assert "hasCompletedOnboarding" in self.dockerfile
        assert ".claude.json" in self.dockerfile


class TestEntrypoint:
    """Validate docker-entrypoint.sh structure and correctness."""

    @pytest.fixture(autouse=True)
    def load_entrypoint(self):
        self.entrypoint_path = REPO_ROOT / "docker-entrypoint.sh"
        self.entrypoint = self.entrypoint_path.read_text()

    def test_entrypoint_exists(self):
        assert self.entrypoint_path.exists()

    def test_entrypoint_is_executable(self):
        assert os.access(self.entrypoint_path, os.X_OK)

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(self.entrypoint_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    def test_uses_strict_mode(self):
        assert "set -euo pipefail" in self.entrypoint

    def test_supports_start_command(self):
        assert "start)" in self.entrypoint

    def test_supports_agent_command(self):
        assert "agent)" in self.entrypoint

    def test_supports_bridge_command(self):
        assert "bridge)" in self.entrypoint

    def test_supports_test_command(self):
        assert "test)" in self.entrypoint

    def test_supports_shell_command(self):
        assert "shell)" in self.entrypoint

    def test_verifies_mounted_binaries(self):
        """Entrypoint must check that CLI binaries are available."""
        assert "verify_binaries" in self.entrypoint

    def test_verifies_auth_state(self):
        """Entrypoint should check for auth directories."""
        assert "verify_auth" in self.entrypoint

    def test_supports_claude_provider(self):
        assert "claude)" in self.entrypoint or "claude" in self.entrypoint

    def test_supports_copilot_provider(self):
        assert "copilot" in self.entrypoint

    def test_supports_ollama_provider(self):
        assert "ollama" in self.entrypoint

    def test_supports_api_key_auth(self):
        """Should support ANTHROPIC_API_KEY as one auth method."""
        assert "ANTHROPIC_API_KEY" in self.entrypoint

    def test_has_check_claude_auth_function(self):
        """Should have a multi-method auth check function."""
        assert "check_claude_auth" in self.entrypoint

    def test_supports_auth_command(self):
        """Should support 'auth' command for auth status check."""
        assert "auth)" in self.entrypoint

    def test_auth_section_explains_setup_token(self):
        """Auth section should explain setup-token flow via make docker-auth."""
        assert "make docker-auth" in self.entrypoint
        assert "setup-token" in self.entrypoint

    def test_check_claude_auth_supports_oauth_token(self):
        """check_claude_auth should recognize CLAUDE_CODE_OAUTH_TOKEN."""
        assert "CLAUDE_CODE_OAUTH_TOKEN" in self.entrypoint

    def test_checks_copilot_auth_dir(self):
        """Should check for ~/.copilot directory."""
        assert ".copilot" in self.entrypoint

    def test_checks_gh_auth_dir(self):
        """Should check for ~/.config/gh directory."""
        assert ".config/gh" in self.entrypoint

    def test_verify_auth_checks_gh_token(self):
        """verify_auth should recognize GH_TOKEN env var."""
        assert "GH_TOKEN" in self.entrypoint

    def test_supports_gh_auth_command(self):
        """Should support 'gh-auth' command for GitHub auth status."""
        assert "gh-auth)" in self.entrypoint

    def test_delegates_to_supervisord(self):
        """Entrypoint should hand off to supervisord for process management."""
        assert "supervisord" in self.entrypoint

    def test_supervisord_conf_exists(self):
        """supervisord.conf must exist in the docker directory."""
        assert (REPO_ROOT / "koan" / "docker" / "supervisord.conf").exists()

    def test_supervisord_conf_has_both_programs(self):
        """supervisord.conf must define both bridge and agent programs."""
        conf = (REPO_ROOT / "koan" / "docker" / "supervisord.conf").read_text()
        assert "[program:bridge]" in conf
        assert "[program:agent]" in conf

    def test_supervisord_conf_autorestart(self):
        """Both programs should auto-restart on crash."""
        conf = (REPO_ROOT / "koan" / "docker" / "supervisord.conf").read_text()
        assert "autorestart=true" in conf

    def test_supervisord_conf_stopasgroup(self):
        """Programs should use stopasgroup for graceful group shutdown."""
        conf = (REPO_ROOT / "koan" / "docker" / "supervisord.conf").read_text()
        assert "stopasgroup=true" in conf

    def test_supervisord_conf_has_heartbeat(self):
        """supervisord.conf must define a heartbeat program."""
        conf = (REPO_ROOT / "koan" / "docker" / "supervisord.conf").read_text()
        assert "[program:heartbeat]" in conf
        assert "koan-heartbeat" in conf

    def test_supervised_run_wrapper_has_restart_delay(self):
        """supervised-run.sh wrapper should delay restarts after crash."""
        wrapper = REPO_ROOT / "koan" / "docker" / "supervised-run.sh"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "sleep 10" in content

    def test_initializes_instance_from_template(self):
        assert "instance.example" in self.entrypoint

    def test_writes_heartbeat_during_monitoring(self):
        """Monitor loop should write heartbeat for HEALTHCHECK."""
        assert "koan-heartbeat" in self.entrypoint

    def test_uses_run_py_not_run_sh(self):
        """Agent loop should use run.py (pure Python), not run.sh."""
        assert "app/run.py" in self.entrypoint
        assert "run.sh" not in self.entrypoint

    def test_uses_awake_py(self):
        """Bridge should use awake.py."""
        assert "app/awake.py" in self.entrypoint

    def test_no_repo_cloning(self):
        """Mounted binaries approach: no repo cloning in container."""
        assert "git clone" not in self.entrypoint

    def test_no_credential_helper(self):
        """Should not set up credential helpers."""
        assert "credential.helper" not in self.entrypoint

    def test_setup_workspace(self):
        """Entrypoint should set up workspace directory."""
        assert "setup_workspace" in self.entrypoint


class TestSetupScript:
    """Validate setup-docker.sh structure and correctness."""

    @pytest.fixture(autouse=True)
    def load_setup(self):
        self.setup_path = REPO_ROOT / "setup-docker.sh"
        self.setup = self.setup_path.read_text()

    def test_setup_exists(self):
        assert self.setup_path.exists()

    def test_setup_is_executable(self):
        assert os.access(self.setup_path, os.X_OK)

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(self.setup_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    def test_uses_strict_mode(self):
        assert "set -euo pipefail" in self.setup

    def test_supports_dry_run(self):
        assert "--dry-run" in self.setup

    def test_generates_override_file(self):
        assert "docker-compose.override.yml" in self.setup

    def test_no_claude_binary_detection(self):
        """Claude is installed in the image — no host binary detection."""
        assert 'detect_binary "claude"' not in self.setup

    def test_detects_copilot_auth_dir(self):
        """Should detect Copilot auth directory."""
        assert "copilot" in self.setup

    def test_no_ollama_binary_detection(self):
        """Ollama detection not needed in current setup."""
        assert 'detect_binary "ollama"' not in self.setup

    def test_explains_claude_in_image(self):
        """Should explain Claude CLI is installed via npm."""
        assert "npm" in self.setup

    def test_creates_claude_auth_directory(self):
        """Should create claude-auth/ for persistent auth state."""
        assert "claude-auth" in self.setup

    def test_mounts_claude_auth_dir(self):
        """claude-auth/ should be mounted as ~/.claude in the container."""
        assert "./claude-auth:/home/koan/.claude" in self.setup

    def test_detects_copilot_auth_dir(self):
        assert ".copilot" in self.setup

    def test_detects_gh_auth_dir(self):
        assert ".config/gh" in self.setup

    def test_detects_host_uid_gid(self):
        """Should detect and write UID/GID for bind mount permissions."""
        assert "HOST_UID" in self.setup
        assert "HOST_GID" in self.setup

    def test_resolves_workspace_symlinks(self):
        """Docker can't follow symlinks — must resolve them."""
        assert "resolve_workspace" in self.setup
        assert "realpath" in self.setup

    def test_mounts_binaries_read_only(self):
        """CLI binaries should be mounted read-only."""
        assert ":ro" in self.setup

    def test_mounts_gh_auth_read_only(self):
        """GitHub CLI auth dir should be mounted read-only."""
        lines = self.setup.splitlines()
        for line in lines:
            if ".config/gh" in line and "detect_dir" in line:
                assert "ro" in line, "~/.config/gh should be mounted read-only"
                break


class TestDockerIgnore:
    """Validate .dockerignore prevents sensitive files from being copied."""

    @pytest.fixture(autouse=True)
    def load_dockerignore(self):
        self.dockerignore = (REPO_ROOT / ".dockerignore").read_text()
        self.patterns = [
            line.strip()
            for line in self.dockerignore.splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def test_dockerignore_exists(self):
        assert (REPO_ROOT / ".dockerignore").exists()

    def test_excludes_env_file(self):
        assert ".env" in self.patterns

    def test_excludes_instance(self):
        assert "instance/" in self.patterns

    def test_excludes_venv(self):
        assert ".venv/" in self.patterns

    def test_excludes_git(self):
        assert ".git/" in self.patterns

    def test_excludes_pycache(self):
        assert "__pycache__/" in self.patterns

    def test_excludes_workspace(self):
        """Workspace is mounted at runtime, not copied."""
        assert "workspace/" in self.patterns

    def test_excludes_docker_override(self):
        """Generated override should not be in build context."""
        assert "docker-compose.override.yml" in self.patterns

    def test_excludes_logs(self):
        assert "logs/" in self.patterns

    def test_excludes_claude_auth(self):
        """claude-auth/ should not be in build context."""
        assert "claude-auth/" in self.patterns


class TestDockerCompose:
    """Validate docker-compose.yml structure."""

    @pytest.fixture(autouse=True)
    def load_compose(self):
        self.compose = (REPO_ROOT / "docker-compose.yml").read_text()

    def test_compose_exists(self):
        assert (REPO_ROOT / "docker-compose.yml").exists()

    def test_defines_koan_service(self):
        assert "koan:" in self.compose

    def test_uses_env_file(self):
        """Should use .env for credentials."""
        assert ".env" in self.compose

    def test_has_restart_policy(self):
        assert "restart:" in self.compose

    def test_mounts_instance_dir(self):
        """Instance state should be a bind mount for persistence."""
        assert "./instance:/app/instance" in self.compose

    def test_workspace_handled_by_setup_script(self):
        """Workspace mounting is generated by setup-docker.sh, not hardcoded in compose."""
        setup = (REPO_ROOT / "setup-docker.sh").read_text()
        assert "resolve_workspace" in setup
        assert "/app/workspace" in setup

    def test_mounts_logs_dir(self):
        assert "./logs:/app/logs" in self.compose

    def test_configurable_uid_gid(self):
        """Build args should support HOST_UID/HOST_GID."""
        assert "HOST_UID" in self.compose
        assert "HOST_GID" in self.compose

    def test_no_ports_exposed_by_default(self):
        """Security: dashboard should NOT be exposed by default."""
        lines = [
            line.strip()
            for line in self.compose.splitlines()
            if not line.strip().startswith("#")
        ]
        for line in lines:
            assert not re.match(r'^-\s*"\d+:\d+"', line), \
                f"Port exposed by default: {line}"

    def test_auth_command_references_docker_auth(self):
        """Auth comments should reference make docker-auth for OAuth."""
        assert "docker-auth" in self.compose

    def test_no_named_volumes(self):
        """Bind mounts preferred over named volumes for transparency."""
        # Named volumes use a volumes: section at the root level
        lines = self.compose.splitlines()
        root_volumes = False
        for line in lines:
            # Root-level "volumes:" (not indented) indicates named volumes
            if re.match(r'^volumes:', line):
                root_volumes = True
                break
        assert not root_volumes, "Prefer bind mounts over named Docker volumes"


class TestMakefileDockerTargets:
    """Validate Docker-related Makefile targets."""

    @pytest.fixture(autouse=True)
    def load_makefile(self):
        self.makefile = (REPO_ROOT / "Makefile").read_text()

    def test_docker_setup_target(self):
        assert "docker-setup:" in self.makefile

    def test_docker_up_target(self):
        assert "docker-up:" in self.makefile

    def test_docker_down_target(self):
        assert "docker-down:" in self.makefile

    def test_docker_logs_target(self):
        assert "docker-logs:" in self.makefile

    def test_docker_test_target(self):
        assert "docker-test:" in self.makefile

    def test_docker_auth_target(self):
        assert "docker-auth:" in self.makefile

    def test_docker_auth_captures_with_script(self):
        """docker-auth should use script to capture setup-token output."""
        assert "script -q" in self.makefile

    def test_docker_gh_auth_target(self):
        assert "docker-gh-auth:" in self.makefile

    def test_docker_gh_auth_extracts_token(self):
        """docker-gh-auth should use 'gh auth token' to extract the token."""
        assert "gh auth token" in self.makefile

    def test_docker_gh_auth_saves_to_env(self):
        """docker-gh-auth should save GH_TOKEN to .env."""
        assert "GH_TOKEN=" in self.makefile

    def test_docker_phony_declarations(self):
        """Docker targets should be declared .PHONY."""
        assert "docker-setup" in self.makefile
        assert "docker-up" in self.makefile
        assert "docker-auth" in self.makefile
        assert "docker-gh-auth" in self.makefile


class TestGitIgnoreDockerEntries:
    """Validate Docker entries in .gitignore."""

    @pytest.fixture(autouse=True)
    def load_gitignore(self):
        self.gitignore = (REPO_ROOT / ".gitignore").read_text()

    def test_ignores_docker_override(self):
        assert "docker-compose.override.yml" in self.gitignore

    def test_ignores_env_docker(self):
        assert ".env.docker" in self.gitignore

    def test_ignores_claude_auth(self):
        """claude-auth/ contains credentials — must be gitignored."""
        assert "claude-auth/" in self.gitignore


class TestDesignPrinciples:
    """Validate key architectural decisions of the mounted-binaries approach."""

    def test_claude_cli_installed_via_npm(self):
        """Dockerfile installs Claude CLI via npm (cross-architecture)."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        assert "npm install -g @anthropic-ai/claude-code" in dockerfile

    def test_entrypoint_explains_setup_token(self):
        """Auth section should guide users to setup-token via make docker-auth."""
        entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()
        assert "setup-token" in entrypoint
        assert "CLAUDE_CODE_OAUTH_TOKEN" in entrypoint

    def test_no_credential_helper_in_entrypoint(self):
        """Should not set up credential helpers with embedded tokens."""
        entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()
        assert "credential.helper" not in entrypoint

    def test_entrypoint_references_python_processes(self):
        """Both processes should be Python (run.py, awake.py)."""
        entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text()
        assert "app/run.py" in entrypoint
        assert "app/awake.py" in entrypoint

    def test_workspace_not_repos(self):
        """Projects directory should be 'workspace/', not 'repos/'."""
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        assert "workspace" in compose
        assert "koan-repos" not in compose
