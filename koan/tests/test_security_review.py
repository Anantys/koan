"""Tests for koan/app/security_review.py — differential security review."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.security_review import (
    classify_file_sensitivity,
    scan_diff_for_patterns,
    calculate_blast_radius,
    assess_risk_level,
    get_diff_against_base,
    get_changed_files,
    check_security_review,
    _severity_meets_threshold,
    _write_journal_entry,
    SENSITIVE_FILE_PATTERNS,
    SENSITIVE_CONTENT_PATTERNS,
    RISK_THRESHOLDS,
)


# ---------------------------------------------------------------------------
# classify_file_sensitivity
# ---------------------------------------------------------------------------


class TestClassifyFileSensitivity:
    """Tests for classify_file_sensitivity()."""

    def test_env_file(self):
        assert classify_file_sensitivity(".env") is True

    def test_env_local(self):
        assert classify_file_sensitivity(".env.local") is True

    def test_secret_file(self):
        assert classify_file_sensitivity("secrets.json") is True

    def test_credential_file(self):
        assert classify_file_sensitivity("credentials.yaml") is True

    def test_auth_module(self):
        assert classify_file_sensitivity("src/auth.py") is True

    def test_dockerfile(self):
        assert classify_file_sensitivity("Dockerfile") is True

    def test_docker_compose(self):
        assert classify_file_sensitivity("docker-compose.yml") is True

    def test_requirements(self):
        assert classify_file_sensitivity("requirements.txt") is True

    def test_pyproject(self):
        assert classify_file_sensitivity("pyproject.toml") is True

    def test_package_json(self):
        assert classify_file_sensitivity("package.json") is True

    def test_makefile(self):
        assert classify_file_sensitivity("Makefile") is True

    def test_sql_file(self):
        assert classify_file_sensitivity("migrations/001.sql") is True

    def test_pem_file(self):
        assert classify_file_sensitivity("certs/server.pem") is True

    def test_key_file(self):
        assert classify_file_sensitivity("ssl/private.key") is True

    def test_regular_python_file(self):
        assert classify_file_sensitivity("src/utils.py") is False

    def test_regular_js_file(self):
        assert classify_file_sensitivity("src/app.js") is False

    def test_readme(self):
        assert classify_file_sensitivity("README.md") is False

    def test_test_file(self):
        assert classify_file_sensitivity("tests/test_main.py") is False

    def test_config_yaml(self):
        assert classify_file_sensitivity("config.yaml") is True

    def test_config_yml(self):
        assert classify_file_sensitivity("app/config.yml") is True

    def test_token_file(self):
        assert classify_file_sensitivity("token.json") is True

    def test_password_file(self):
        assert classify_file_sensitivity("password_reset.py") is True


# ---------------------------------------------------------------------------
# scan_diff_for_patterns
# ---------------------------------------------------------------------------


class TestScanDiffForPatterns:
    """Tests for scan_diff_for_patterns()."""

    def test_detects_eval(self):
        diff = "+result = eval(user_input)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "eval() usage"

    def test_detects_exec(self):
        diff = "+exec(code_string)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "exec() usage"

    def test_detects_shell_true(self):
        diff = "+subprocess.run(cmd, shell=True)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "shell=True subprocess"

    def test_detects_os_system(self):
        diff = "+os.system('rm -rf /')"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "os.system() usage"

    def test_detects_hardcoded_secret(self):
        diff = "+api_key = 'sk-1234567890'"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "hardcoded secret"

    def test_detects_pickle_loads(self):
        diff = "+data = pickle.loads(raw)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "unsafe deserialization"

    def test_detects_innerhtml(self):
        diff = "+element.innerHTML = userInput"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "potential XSS via innerHTML"

    def test_detects_dangerously_set_innerhtml(self):
        diff = "+<div dangerouslySetInnerHTML={{__html: data}} />"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "React XSS risk"

    def test_detects_chmod_777(self):
        diff = "+chmod 777 /tmp/myfile"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "overly permissive file permissions"

    def test_detects_no_verify(self):
        diff = "+git commit --no-verify"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "verification bypass"

    def test_detects_wildcard_cors(self):
        diff = "+Access-Control-Allow-Origin: *"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "wildcard CORS"

    def test_detects_ssl_disable(self):
        diff = "+disable_ssl_verify = True"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "SSL/TLS verification disabled"

    def test_ignores_removed_lines(self):
        diff = "-result = eval(user_input)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 0

    def test_ignores_context_lines(self):
        diff = " result = eval(user_input)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 0

    def test_ignores_diff_header(self):
        diff = "+++ b/src/main.py"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 0

    def test_multiple_findings(self):
        diff = "+eval(x)\n+os.system('cmd')"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 2

    def test_empty_diff(self):
        assert scan_diff_for_patterns("") == []

    def test_safe_diff(self):
        diff = "+x = 1 + 2\n+print(x)"
        assert scan_diff_for_patterns(diff) == []


# ---------------------------------------------------------------------------
# calculate_blast_radius
# ---------------------------------------------------------------------------


class TestCalculateBlastRadius:
    """Tests for calculate_blast_radius()."""

    def test_empty_files(self):
        result = calculate_blast_radius([])
        assert result["file_count"] == 0
        assert result["sensitive_file_count"] == 0
        assert result["has_infra_changes"] is False
        assert result["has_dependency_changes"] is False

    def test_single_safe_file(self):
        result = calculate_blast_radius(["src/main.py"])
        assert result["file_count"] == 1
        assert result["sensitive_file_count"] == 0

    def test_sensitive_files_counted(self):
        result = calculate_blast_radius([".env", "src/auth.py", "src/main.py"])
        assert result["file_count"] == 3
        assert result["sensitive_file_count"] == 2
        assert ".env" in result["sensitive_files"]
        assert "src/auth.py" in result["sensitive_files"]

    def test_modules_affected(self):
        result = calculate_blast_radius([
            "src/main.py", "src/utils.py",
            "tests/test_main.py",
            "docs/readme.md",
        ])
        assert set(result["modules_affected"]) == {"src", "tests", "docs"}

    def test_infra_changes_detected(self):
        result = calculate_blast_radius(["Dockerfile", "src/main.py"])
        assert result["has_infra_changes"] is True

    def test_dependency_changes_detected(self):
        result = calculate_blast_radius(["requirements.txt", "src/main.py"])
        assert result["has_dependency_changes"] is True

    def test_no_infra_or_deps(self):
        result = calculate_blast_radius(["src/main.py", "src/utils.py"])
        assert result["has_infra_changes"] is False
        assert result["has_dependency_changes"] is False

    def test_docker_compose_infra(self):
        result = calculate_blast_radius(["docker-compose.yml"])
        assert result["has_infra_changes"] is True

    def test_package_json_deps(self):
        result = calculate_blast_radius(["package.json"])
        assert result["has_dependency_changes"] is True

    def test_root_files_no_module(self):
        result = calculate_blast_radius(["README.md"])
        assert result["modules_affected"] == []


# ---------------------------------------------------------------------------
# assess_risk_level
# ---------------------------------------------------------------------------


class TestAssessRiskLevel:
    """Tests for assess_risk_level()."""

    def test_low_risk_minimal_changes(self):
        br = {"file_count": 1, "sensitive_file_count": 0,
              "has_infra_changes": False, "has_dependency_changes": False,
              "modules_affected": ["src"]}
        risk, score = assess_risk_level(br, [])
        assert risk == "low"

    def test_medium_risk_several_files(self):
        br = {"file_count": 8, "sensitive_file_count": 1,
              "has_infra_changes": False, "has_dependency_changes": False,
              "modules_affected": ["src", "tests"]}
        risk, score = assess_risk_level(br, [])
        assert risk == "medium"

    def test_high_risk_infra_and_findings(self):
        br = {"file_count": 5, "sensitive_file_count": 1,
              "has_infra_changes": True, "has_dependency_changes": True,
              "modules_affected": ["src", "tests", "infra", "docs"]}
        findings = [("eval() usage", "eval(x)", "eval(x)")]
        risk, score = assess_risk_level(br, findings)
        assert risk in ("high", "critical")

    def test_critical_risk_many_findings(self):
        br = {"file_count": 25, "sensitive_file_count": 3,
              "has_infra_changes": True, "has_dependency_changes": True,
              "modules_affected": ["a", "b", "c", "d"]}
        findings = [("f", "m", "l")] * 5
        risk, score = assess_risk_level(br, findings)
        assert risk == "critical"

    def test_content_findings_add_score(self):
        br = {"file_count": 1, "sensitive_file_count": 0,
              "has_infra_changes": False, "has_dependency_changes": False,
              "modules_affected": []}
        _, score_without = assess_risk_level(br, [])
        _, score_with = assess_risk_level(br, [("x", "y", "z")])
        assert score_with > score_without

    def test_sensitive_files_add_score(self):
        br_none = {"file_count": 2, "sensitive_file_count": 0,
                   "has_infra_changes": False, "has_dependency_changes": False,
                   "modules_affected": []}
        br_some = {"file_count": 2, "sensitive_file_count": 2,
                   "has_infra_changes": False, "has_dependency_changes": False,
                   "modules_affected": []}
        _, score_none = assess_risk_level(br_none, [])
        _, score_some = assess_risk_level(br_some, [])
        assert score_some > score_none

    def test_empty_blast_radius_is_low(self):
        br = {"file_count": 0, "sensitive_file_count": 0,
              "has_infra_changes": False, "has_dependency_changes": False,
              "modules_affected": []}
        risk, score = assess_risk_level(br, [])
        assert risk == "low"
        assert score == 0


# ---------------------------------------------------------------------------
# _severity_meets_threshold
# ---------------------------------------------------------------------------


class TestSeverityMeetsThreshold:
    """Tests for _severity_meets_threshold()."""

    def test_critical_meets_high(self):
        assert _severity_meets_threshold("critical", "high") is True

    def test_high_meets_high(self):
        assert _severity_meets_threshold("high", "high") is True

    def test_medium_does_not_meet_high(self):
        assert _severity_meets_threshold("medium", "high") is False

    def test_low_does_not_meet_medium(self):
        assert _severity_meets_threshold("low", "medium") is False

    def test_high_meets_low(self):
        assert _severity_meets_threshold("high", "low") is True

    def test_low_meets_low(self):
        assert _severity_meets_threshold("low", "low") is True

    def test_critical_meets_critical(self):
        assert _severity_meets_threshold("critical", "critical") is True

    def test_high_does_not_meet_critical(self):
        assert _severity_meets_threshold("high", "critical") is False


# ---------------------------------------------------------------------------
# get_diff_against_base / get_changed_files
# ---------------------------------------------------------------------------


class TestGitHelpers:
    """Tests for git-based helper functions."""

    @patch("app.security_review._run_git")
    def test_get_diff_upstream_first(self, mock_git):
        mock_git.return_value = "diff content"
        result = get_diff_against_base("/project", "main")
        assert result == "diff content"
        mock_git.assert_called_once_with("/project", "diff", "upstream/main...HEAD")

    @patch("app.security_review._run_git")
    def test_get_diff_falls_back_to_origin(self, mock_git):
        mock_git.side_effect = ["", "origin diff"]
        result = get_diff_against_base("/project", "main")
        assert result == "origin diff"

    @patch("app.security_review._run_git")
    def test_get_diff_falls_back_to_bare(self, mock_git):
        mock_git.side_effect = ["", "", "bare diff"]
        result = get_diff_against_base("/project", "main")
        assert result == "bare diff"

    @patch("app.security_review._run_git")
    def test_get_diff_returns_empty_when_all_fail(self, mock_git):
        mock_git.return_value = ""
        result = get_diff_against_base("/project", "main")
        assert result == ""

    @patch("app.security_review._run_git")
    def test_get_changed_files_parses_output(self, mock_git):
        mock_git.return_value = "src/main.py\nsrc/utils.py\n"
        result = get_changed_files("/project", "main")
        assert result == ["src/main.py", "src/utils.py"]

    @patch("app.security_review._run_git")
    def test_get_changed_files_empty(self, mock_git):
        mock_git.return_value = ""
        result = get_changed_files("/project", "main")
        assert result == []


# ---------------------------------------------------------------------------
# _write_journal_entry
# ---------------------------------------------------------------------------


class TestWriteJournalEntry:
    """Tests for _write_journal_entry()."""

    @patch("app.utils.write_to_journal")
    def test_writes_entry(self, mock_write):
        _write_journal_entry(
            "/instance", "myapp", "high", 15,
            {"file_count": 5, "sensitive_file_count": 1,
             "modules_affected": ["src"], "has_infra_changes": False,
             "has_dependency_changes": False},
            [("eval() usage", "eval(x)", "eval(x)")],
            blocked=False,
        )
        mock_write.assert_called_once()
        entry = mock_write.call_args[0][1]
        assert "high" in entry
        assert "eval() usage" in entry

    @patch("app.utils.write_to_journal")
    def test_blocked_entry(self, mock_write):
        _write_journal_entry(
            "/instance", "myapp", "critical", 25,
            {"file_count": 30, "sensitive_file_count": 5,
             "modules_affected": ["a", "b"], "has_infra_changes": True,
             "has_dependency_changes": True},
            [], blocked=True,
        )
        entry = mock_write.call_args[0][1]
        assert "blocked" in entry.lower()

    @patch("app.utils.write_to_journal")
    def test_truncates_many_findings(self, mock_write):
        findings = [(f"finding_{i}", f"m_{i}", f"ctx_{i}") for i in range(15)]
        _write_journal_entry(
            "/instance", "myapp", "high", 20,
            {"file_count": 1, "sensitive_file_count": 0,
             "modules_affected": [], "has_infra_changes": False,
             "has_dependency_changes": False},
            findings, blocked=False,
        )
        entry = mock_write.call_args[0][1]
        assert "5 more" in entry

    @patch("app.utils.write_to_journal", side_effect=Exception("fail"))
    def test_handles_write_failure(self, mock_write):
        # Should not raise
        _write_journal_entry(
            "/instance", "myapp", "low", 0,
            {"file_count": 0, "sensitive_file_count": 0,
             "modules_affected": [], "has_infra_changes": False,
             "has_dependency_changes": False},
            [], blocked=False,
        )


# ---------------------------------------------------------------------------
# check_security_review (integration)
# ---------------------------------------------------------------------------


class TestCheckSecurityReview:
    """Integration tests for check_security_review()."""

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_disabled_returns_true(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": False}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is True
        mock_diff.assert_not_called()

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_no_config_returns_true(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = None
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is True

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_no_changes_returns_true(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": True}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = []
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is True

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_low_risk_passes(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": True, "blocking": True}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+x = 1"
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is True

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_high_risk_non_blocking_passes(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": True, "blocking": False}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = [".env", "Dockerfile", "src/auth.py"] + [f"f{i}.py" for i in range(20)]
        mock_diff.return_value = "\n".join([
            "+eval(x)", "+os.system('cmd')", "+subprocess.run(x, shell=True)",
            "+api_key = 'secret123'", "+pickle.loads(data)",
        ])
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is True  # Non-blocking mode

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_high_risk_blocking_blocks(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {
                "enabled": True, "blocking": True, "severity_threshold": "high",
            }},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = [".env", "Dockerfile", "src/auth.py"] + [f"f{i}.py" for i in range(20)]
        mock_diff.return_value = "\n".join([
            "+eval(x)", "+os.system('cmd')", "+subprocess.run(x, shell=True)",
            "+api_key = 'secret123'", "+pickle.loads(data)",
        ])
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is False  # Blocking mode, high risk

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_blocking_low_threshold_blocks_medium(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {
                "enabled": True, "blocking": True, "severity_threshold": "medium",
            }},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        # Enough changes for medium risk (6+ score):
        # Dockerfile -> infra (+3), requirements.txt -> deps (+2), auth.py -> sensitive (+3)
        # = 8 score (medium is 6+)
        mock_files.return_value = ["src/main.py", "Dockerfile", "requirements.txt",
                                   "src/auth.py"]
        mock_diff.return_value = "+x = 1"
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is False  # Medium risk meets medium threshold

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_uses_base_branch_from_auto_merge_config(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {
                "security_review": {"enabled": True},
                "git_auto_merge": {"base_branch": "develop"},
            },
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+x = 1"
        check_security_review("/instance", "myapp", "/tmp/myapp")
        mock_files.assert_called_with("/tmp/myapp", "develop")

    @patch("app.utils.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_per_project_override(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": False}},
            "projects": {"myapp": {
                "path": "/tmp/myapp",
                "security_review": {"enabled": True, "blocking": True},
            }},
        }
        mock_files.return_value = [".env", "Dockerfile"] + [f"f{i}.py" for i in range(20)]
        mock_diff.return_value = "+eval(x)\n+os.system('cmd')\n+api_key='s'"
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result is False  # Per-project override enables blocking


# ---------------------------------------------------------------------------
# mission_runner integration
# ---------------------------------------------------------------------------


class TestMissionRunnerIntegration:
    """Tests for check_security_review wrapper in mission_runner."""

    @patch("app.security_review.check_security_review", return_value=True)
    def test_wrapper_returns_true(self, mock_check):
        from app.mission_runner import check_security_review as wrapper
        result = wrapper("/instance", "myapp", "/tmp/myapp")
        assert result is True

    @patch("app.security_review.check_security_review", return_value=False)
    def test_wrapper_returns_false(self, mock_check):
        from app.mission_runner import check_security_review as wrapper
        result = wrapper("/instance", "myapp", "/tmp/myapp")
        assert result is False

    @patch("app.security_review.check_security_review", side_effect=Exception("boom"))
    def test_wrapper_returns_true_on_error(self, mock_check):
        from app.mission_runner import check_security_review as wrapper
        result = wrapper("/instance", "myapp", "/tmp/myapp")
        assert result is True  # Fail-open


# ---------------------------------------------------------------------------
# projects_config accessor
# ---------------------------------------------------------------------------


class TestGetProjectSecurityReview:
    """Tests for get_project_security_review() in projects_config."""

    def test_defaults_when_not_configured(self):
        from app.projects_config import get_project_security_review
        config = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        result = get_project_security_review(config, "myapp")
        assert result == {"enabled": False, "blocking": False, "severity_threshold": "high"}

    def test_enabled_from_defaults(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {"enabled": True}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result["enabled"] is True
        assert result["blocking"] is False

    def test_full_config(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {
                "enabled": True, "blocking": True, "severity_threshold": "medium",
            }},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result == {"enabled": True, "blocking": True, "severity_threshold": "medium"}

    def test_per_project_override(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {"enabled": False}},
            "projects": {"myapp": {
                "path": "/tmp/myapp",
                "security_review": {"enabled": True, "blocking": True},
            }},
        }
        result = get_project_security_review(config, "myapp")
        assert result["enabled"] is True
        assert result["blocking"] is True

    def test_handles_none_security_review(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": None},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result == {"enabled": False, "blocking": False, "severity_threshold": "high"}

    def test_severity_threshold_normalized(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {"severity_threshold": "  HIGH  "}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result["severity_threshold"] == "high"
