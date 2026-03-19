"""Tests for systemd service PATH building and template rendering."""

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.systemd_service import (
    _validate_placeholder,
    build_safe_path,
    main,
    render_all_templates,
    render_service_template,
)


class TestBuildSafePath:
    """Tests for build_safe_path()."""

    def test_filters_home_dirs(self):
        raw = "/usr/bin:/home/alice/.local/bin:/home/alice/bin:/usr/local/bin"
        result = build_safe_path(raw, "/home/alice")
        assert "/home/alice/.local/bin" in result.split(":")
        assert "/home/alice/bin" not in result.split(":")
        assert "/usr/bin" in result.split(":")
        assert "/usr/local/bin" in result.split(":")

    def test_filters_root_home_dirs(self):
        raw = "/root/.local/bin:/root/.cargo/bin:/usr/bin:/usr/local/bin"
        result = build_safe_path(raw, "/root")
        assert "/root/.local/bin" in result.split(":")
        assert "/root/.cargo/bin" not in result.split(":")
        assert "/usr/bin" in result.split(":")

    def test_ensures_essentials(self):
        raw = "/opt/custom/bin"
        result = build_safe_path(raw, "/home/user")
        parts = result.split(":")
        for d in ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]:
            assert d in parts, f"essential dir {d} missing"

    def test_deduplicates(self):
        raw = "/usr/bin:/usr/bin:/usr/local/bin:/usr/local/bin"
        result = build_safe_path(raw, "/home/user")
        parts = result.split(":")
        assert parts.count("/usr/bin") == 1
        assert parts.count("/usr/local/bin") == 1

    def test_preserves_order(self):
        raw = "/opt/cpanel/bin:/usr/local/cpanel/bin:/var/opt/bin:/usr/bin"
        result = build_safe_path(raw, "/home/user")
        parts = result.split(":")
        # Original entries should appear before appended essentials
        assert parts.index("/opt/cpanel/bin") < parts.index("/usr/local/cpanel/bin")
        assert parts.index("/usr/local/cpanel/bin") < parts.index("/var/opt/bin")

    def test_empty_input_gets_essentials(self):
        result = build_safe_path("", "/home/user")
        parts = result.split(":")
        assert len(parts) == 6
        for d in ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]:
            assert d in parts

    def test_sudo_stripped_path_scenario(self):
        """Simulates the bug: sudo resets PATH to minimal set."""
        sudo_path = "/sbin:/bin:/usr/sbin:/usr/bin"
        caller_path = (
            "/root/.local/bin:/root/.dotfiles/bin:"
            "/usr/local/cpanel/bin:/usr/local/cpanel/3rdparty/bin:"
            "/root/.cargo/bin:/usr/local/cpanel/3rdparty/node/22/bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        # With sudo-stripped PATH (the bug)
        bad = build_safe_path(sudo_path, "/root")
        assert "/usr/local/cpanel/bin" not in bad.split(":")

        # With caller's full PATH (the fix)
        good = build_safe_path(caller_path, "/root")
        parts = good.split(":")
        assert "/usr/local/cpanel/bin" in parts
        assert "/usr/local/cpanel/3rdparty/bin" in parts
        assert "/usr/local/cpanel/3rdparty/node/22/bin" in parts
        # Allowed home subdirs preserved, others filtered
        assert "/root/.local/bin" in parts
        assert "/root/.cargo/bin" not in parts

    def test_trailing_slash_on_home(self):
        raw = "/root/.local/bin:/root/.cargo/bin:/usr/bin"
        result = build_safe_path(raw, "/root/")
        assert "/root/.local/bin" in result.split(":")
        assert "/root/.cargo/bin" not in result.split(":")

    def test_allows_local_bin(self):
        """~/.local/bin is preserved while other home subdirs are filtered."""
        raw = "/home/user/.local/bin:/home/user/.cargo/bin:/home/user/.nvm/bin:/usr/bin"
        result = build_safe_path(raw, "/home/user")
        parts = result.split(":")
        assert "/home/user/.local/bin" in parts
        assert "/home/user/.cargo/bin" not in parts
        assert "/home/user/.nvm/bin" not in parts
        assert "/usr/bin" in parts

    def test_exact_home_dir_filtered(self):
        """Home dir itself (not just subdirs) should be filtered."""
        raw = "/home/user:/usr/bin"
        result = build_safe_path(raw, "/home/user")
        assert "/home/user" not in result.split(":")
        assert "/usr/bin" in result.split(":")


class TestValidatePlaceholder:
    """Tests for _validate_placeholder."""

    def test_accepts_normal_values(self):
        assert _validate_placeholder("test", "/opt/koan") == "/opt/koan"

    def test_rejects_newlines(self):
        with pytest.raises(ValueError, match="newlines"):
            _validate_placeholder("test", "/opt/koan\nExecStart=/bin/malicious")

    def test_rejects_carriage_return(self):
        with pytest.raises(ValueError, match="newlines"):
            _validate_placeholder("test", "/opt/koan\r\nExecStart=/bin/bad")


class TestRenderServiceTemplate:
    """Tests for template rendering."""

    def test_render_replaces_placeholders(self, tmp_path):
        template = tmp_path / "koan.service.template"
        template.write_text(textwrap.dedent("""\
            [Service]
            WorkingDirectory=__KOAN_ROOT__/koan
            Environment=PATH=__PATH__
            ExecStart=__PYTHON__ app/run.py
        """))

        result = render_service_template(
            str(template), "/opt/koan", "/opt/koan/.venv/bin/python3",
            "/usr/local/bin:/usr/bin:/bin"
        )

        assert "WorkingDirectory=/opt/koan/koan" in result
        assert "Environment=PATH=/usr/local/bin:/usr/bin:/bin" in result
        assert "ExecStart=/opt/koan/.venv/bin/python3 app/run.py" in result
        assert "__KOAN_ROOT__" not in result
        assert "__PYTHON__" not in result
        assert "__PATH__" not in result

    def test_render_replaces_user_and_group(self, tmp_path):
        template = tmp_path / "koan.service.template"
        template.write_text("User=__USER__\nGroup=__GROUP__\n")

        result = render_service_template(
            str(template), "/opt/koan", "/usr/bin/python3", "/usr/bin",
            user="alice", group="alice",
        )
        assert "User=alice" in result
        assert "Group=alice" in result

    def test_user_defaults_to_root(self, tmp_path):
        template = tmp_path / "koan.service.template"
        template.write_text("User=__USER__\nGroup=__GROUP__\n")

        result = render_service_template(
            str(template), "/opt/koan", "/usr/bin/python3", "/usr/bin",
        )
        assert "User=root" in result
        assert "Group=root" in result

    def test_rejects_newline_injection(self, tmp_path):
        template = tmp_path / "koan.service.template"
        template.write_text("WorkingDirectory=__KOAN_ROOT__\n")

        with pytest.raises(ValueError):
            render_service_template(
                str(template), "/opt/koan\nExecStart=/bin/bad",
                "/usr/bin/python3", "/usr/bin",
            )

    def test_render_all_templates(self, tmp_path):
        for name in ["koan.service.template", "koan-awake.service.template"]:
            (tmp_path / name).write_text("WorkingDirectory=__KOAN_ROOT__\nExecStart=__PYTHON__\nPATH=__PATH__\nUser=__USER__\nGroup=__GROUP__\n")
        # Non-matching file should be ignored
        (tmp_path / "other.conf").write_text("ignore me")

        result = render_all_templates(
            str(tmp_path), "/opt/koan", "/usr/bin/python3", "/usr/bin:/bin"
        )

        assert "koan.service" in result
        assert "koan-awake.service" in result
        assert len(result) == 2
        for content in result.values():
            assert "__KOAN_ROOT__" not in content
            assert "/opt/koan" in content

    def test_render_all_templates_empty_dir(self, tmp_path):
        result = render_all_templates(
            str(tmp_path), "/opt/koan", "/usr/bin/python3", "/usr/bin"
        )
        assert result == {}

    def test_render_all_passes_user_group(self, tmp_path):
        (tmp_path / "koan.service.template").write_text("User=__USER__\nGroup=__GROUP__\n")

        result = render_all_templates(
            str(tmp_path), "/opt/koan", "/usr/bin/python3", "/usr/bin",
            user="deploy", group="deploy",
        )
        assert "User=deploy" in result["koan.service"]
        assert "Group=deploy" in result["koan.service"]


class TestMain:
    """Tests for the CLI entrypoint main()."""

    def test_wrong_argc_exits_with_error(self):
        with patch.object(sys, "argv", ["prog"]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_too_many_args_exits_with_error(self):
        with patch.object(sys, "argv", ["prog", "a", "b", "c", "d", "e"]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_renders_templates_to_output_dir(self, tmp_path):
        # Create a fake app/ dir with a sibling systemd/ dir to match main()'s
        # template_dir = os.path.join(os.path.dirname(__file__), "..", "systemd")
        fake_app = tmp_path / "koan" / "app"
        fake_app.mkdir(parents=True)
        tmpl_dir = tmp_path / "koan" / "systemd"
        tmpl_dir.mkdir()
        (tmpl_dir / "koan.service.template").write_text(
            "WorkingDirectory=__KOAN_ROOT__\nExecStart=__PYTHON__\nPATH=__PATH__\n"
        )
        out_dir = tmp_path / "output"

        with patch.object(
            sys, "argv",
            ["prog", "/opt/koan", "/usr/bin/python3", "/usr/bin:/bin", str(out_dir)]
        ), patch("app.systemd_service.os.path.dirname", return_value=str(fake_app)):
            main()

        assert (out_dir / "koan.service").exists()
        content = (out_dir / "koan.service").read_text()
        assert "/opt/koan" in content
        assert "__KOAN_ROOT__" not in content

    def test_uses_sudo_user_for_home_filtering(self, tmp_path):
        fake_app = tmp_path / "koan" / "app"
        fake_app.mkdir(parents=True)
        tmpl_dir = tmp_path / "koan" / "systemd"
        tmpl_dir.mkdir()
        (tmpl_dir / "koan.service.template").write_text("PATH=__PATH__\n")
        out_dir = tmp_path / "output"

        caller_path = "/home/alice/.local/bin:/usr/bin:/bin"

        with patch.object(
            sys, "argv",
            ["prog", "/opt/koan", "/usr/bin/python3", caller_path, str(out_dir)]
        ), patch.dict(os.environ, {"SUDO_USER": "alice"}, clear=False), \
             patch("app.systemd_service.os.path.dirname", return_value=str(fake_app)), \
             patch("app.systemd_service.os.path.expanduser", return_value="/home/alice"):
            main()

        content = (out_dir / "koan.service").read_text()
        assert "/home/alice/.local/bin" in content
        assert "/usr/bin" in content


class TestServiceTemplateContent:
    """Validate actual service template files have correct systemd directives."""

    @pytest.fixture
    def template_dir(self):
        """Path to the real systemd template directory."""
        return os.path.join(os.path.dirname(__file__), "..", "systemd")

    def test_koan_service_requires_awake(self, template_dir):
        """koan.service must Require koan-awake so 'systemctl start koan' starts both."""
        path = os.path.join(template_dir, "koan.service.template")
        content = Path(path).read_text()
        assert "Requires=koan-awake.service" in content

    def test_koan_service_binds_to_awake(self, template_dir):
        """koan.service must BindTo koan-awake so it stops when awake stops."""
        path = os.path.join(template_dir, "koan.service.template")
        content = Path(path).read_text()
        assert "BindsTo=koan-awake.service" in content

    def test_koan_service_starts_after_awake(self, template_dir):
        """koan.service must start After koan-awake for correct ordering."""
        path = os.path.join(template_dir, "koan.service.template")
        content = Path(path).read_text()
        assert "koan-awake.service" in content
        # After directive should reference koan-awake
        for line in content.splitlines():
            if line.startswith("After="):
                assert "koan-awake.service" in line
                break
        else:
            pytest.fail("No After= directive found")

    def test_awake_service_part_of_koan(self, template_dir):
        """koan-awake.service must be PartOf koan.service for bidirectional lifecycle."""
        path = os.path.join(template_dir, "koan-awake.service.template")
        content = Path(path).read_text()
        assert "PartOf=koan.service" in content

    def test_both_templates_have_required_placeholders(self, template_dir):
        """Both templates must contain all required placeholders."""
        for name in ["koan.service.template", "koan-awake.service.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "__KOAN_ROOT__" in content, f"{name} missing __KOAN_ROOT__"
            assert "__PYTHON__" in content, f"{name} missing __PYTHON__"
            assert "__PATH__" in content, f"{name} missing __PATH__"
            assert "__USER__" in content, f"{name} missing __USER__"
            assert "__GROUP__" in content, f"{name} missing __GROUP__"

    def test_both_templates_have_user_group(self, template_dir):
        """Both templates must have User and Group directives."""
        for name in ["koan.service.template", "koan-awake.service.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "User=__USER__" in content, f"{name} missing User=__USER__"
            assert "Group=__GROUP__" in content, f"{name} missing Group=__GROUP__"

    def test_both_templates_use_env_file(self, template_dir):
        """Both templates must load .env for secrets."""
        for name in ["koan.service.template", "koan-awake.service.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "EnvironmentFile=__KOAN_ROOT__/.env" in content, \
                f"{name} missing EnvironmentFile"

    def test_both_templates_restart_on_failure(self, template_dir):
        """Both services should restart on failure."""
        for name in ["koan.service.template", "koan-awake.service.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "Restart=on-failure" in content, \
                f"{name} missing Restart=on-failure"

    def test_both_templates_have_ssh_auth_sock(self, template_dir):
        """Both templates must set SSH_AUTH_SOCK for git SSH operations."""
        for name in ["koan.service.template", "koan-awake.service.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "SSH_AUTH_SOCK=__KOAN_ROOT__/.ssh-agent-sock" in content, \
                f"{name} missing SSH_AUTH_SOCK environment variable"
