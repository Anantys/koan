"""Tests for launchd plist template rendering."""

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.launchd_service import (
    _validate_placeholder,
    get_launchd_dir,
    main,
    render_all_templates,
    render_plist_template,
)


class TestValidatePlaceholder:
    """Tests for _validate_placeholder."""

    def test_accepts_normal_values(self):
        assert _validate_placeholder("test", "/opt/koan") == "/opt/koan"

    def test_rejects_newlines(self):
        with pytest.raises(ValueError, match="newlines"):
            _validate_placeholder("test", "/opt/koan\n<key>Bad</key>")

    def test_rejects_carriage_return(self):
        with pytest.raises(ValueError, match="newlines"):
            _validate_placeholder("test", "/opt/koan\r\n<key>Bad</key>")


class TestRenderPlistTemplate:
    """Tests for template rendering."""

    def test_render_replaces_placeholders(self, tmp_path):
        template = tmp_path / "com.koan.run.plist.template"
        template.write_text(textwrap.dedent("""\
            <string>__KOAN_ROOT__/koan/launchd/koan-wrapper.sh</string>
            <string>__KOAN_ROOT__/koan</string>
            <string>__KOAN_ROOT__/logs/run.log</string>
        """))

        result = render_plist_template(str(template), "/opt/koan")

        assert "/opt/koan/koan/launchd/koan-wrapper.sh" in result
        assert "/opt/koan/koan" in result
        assert "/opt/koan/logs/run.log" in result
        assert "__KOAN_ROOT__" not in result

    def test_rejects_newline_injection(self, tmp_path):
        template = tmp_path / "com.koan.run.plist.template"
        template.write_text("<string>__KOAN_ROOT__</string>\n")

        with pytest.raises(ValueError):
            render_plist_template(
                str(template), "/opt/koan\n<key>Bad</key>"
            )


class TestRenderAllTemplates:
    """Tests for render_all_templates."""

    def test_renders_matching_templates(self, tmp_path):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            (tmp_path / name).write_text(
                "<string>__KOAN_ROOT__/koan</string>\n"
            )
        # Non-matching file should be ignored
        (tmp_path / "other.plist").write_text("ignore me")

        result = render_all_templates(str(tmp_path), "/opt/koan")

        assert "com.koan.run.plist" in result
        assert "com.koan.awake.plist" in result
        assert len(result) == 2
        for content in result.values():
            assert "__KOAN_ROOT__" not in content
            assert "/opt/koan/koan" in content

    def test_empty_dir_returns_empty(self, tmp_path):
        result = render_all_templates(str(tmp_path), "/opt/koan")
        assert result == {}


class TestGetLaunchdDir:
    """Tests for get_launchd_dir."""

    def test_returns_launch_agents_path(self):
        result = get_launchd_dir()
        assert result.endswith("/Library/LaunchAgents")
        assert os.path.expanduser("~") in result


class TestMain:
    """Tests for the CLI entrypoint main()."""

    def test_wrong_argc_exits_with_error(self):
        with patch.object(sys, "argv", ["prog"]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_too_many_args_exits_with_error(self):
        with patch.object(sys, "argv", ["prog", "a", "b", "c"]):
            with pytest.raises(SystemExit, match="1"):
                main()

    def test_renders_templates_to_output_dir(self, tmp_path):
        fake_app = tmp_path / "koan" / "app"
        fake_app.mkdir(parents=True)
        tmpl_dir = tmp_path / "koan" / "launchd"
        tmpl_dir.mkdir()
        (tmpl_dir / "com.koan.run.plist.template").write_text(
            "<string>__KOAN_ROOT__</string>\n"
        )
        out_dir = tmp_path / "output"

        with patch.object(
            sys, "argv",
            ["prog", "/opt/koan", str(out_dir)]
        ), patch("app.launchd_service.os.path.dirname", return_value=str(fake_app)):
            main()

        assert (out_dir / "com.koan.run.plist").exists()
        content = (out_dir / "com.koan.run.plist").read_text()
        assert "/opt/koan" in content
        assert "__KOAN_ROOT__" not in content


class TestPlistTemplateContent:
    """Validate actual plist template files have correct structure."""

    @pytest.fixture
    def template_dir(self):
        """Path to the real launchd template directory."""
        return os.path.join(os.path.dirname(__file__), "..", "launchd")

    def test_both_templates_exist(self, template_dir):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            assert os.path.exists(path), f"{name} not found"

    def test_both_templates_have_koan_root_placeholder(self, template_dir):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "__KOAN_ROOT__" in content, f"{name} missing __KOAN_ROOT__"

    def test_both_templates_have_run_at_load(self, template_dir):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "<key>RunAtLoad</key>" in content, f"{name} missing RunAtLoad"
            assert "<true/>" in content, f"{name} RunAtLoad not set to true"

    def test_both_templates_have_keep_alive(self, template_dir):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "<key>KeepAlive</key>" in content, f"{name} missing KeepAlive"

    def test_both_templates_have_throttle_interval(self, template_dir):
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            assert "<key>ThrottleInterval</key>" in content, \
                f"{name} missing ThrottleInterval"

    def test_run_template_uses_wrapper_with_run_script(self, template_dir):
        path = os.path.join(template_dir, "com.koan.run.plist.template")
        content = Path(path).read_text()
        assert "koan-wrapper.sh" in content
        assert "app/run.py" in content

    def test_awake_template_uses_wrapper_with_awake_script(self, template_dir):
        path = os.path.join(template_dir, "com.koan.awake.plist.template")
        content = Path(path).read_text()
        assert "koan-wrapper.sh" in content
        assert "app/awake.py" in content

    def test_run_template_logs_to_run_log(self, template_dir):
        path = os.path.join(template_dir, "com.koan.run.plist.template")
        content = Path(path).read_text()
        assert "logs/run.log" in content

    def test_awake_template_logs_to_awake_log(self, template_dir):
        path = os.path.join(template_dir, "com.koan.awake.plist.template")
        content = Path(path).read_text()
        assert "logs/awake.log" in content

    def test_both_templates_are_valid_plist(self, template_dir):
        """Templates should be parseable plist (after placeholder substitution)."""
        import plistlib
        for name in ["com.koan.run.plist.template", "com.koan.awake.plist.template"]:
            path = os.path.join(template_dir, name)
            content = Path(path).read_text()
            content = content.replace("__KOAN_ROOT__", "/opt/koan")
            try:
                plistlib.loads(content.encode())
            except Exception as e:
                pytest.fail(f"{name} is not a valid plist: {e}")

    def test_wrapper_script_exists_and_executable(self, template_dir):
        wrapper = os.path.join(template_dir, "koan-wrapper.sh")
        assert os.path.exists(wrapper), "koan-wrapper.sh not found"
        assert os.access(wrapper, os.X_OK), "koan-wrapper.sh not executable"
