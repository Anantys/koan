"""Tests for koan/app/security_review.py — differential security review."""

import hashlib
import os
import subprocess
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
    SecurityReviewResult,
    _extract_variant_patterns,
    _extract_diff_lines,
    _check_variants_grep,
    _check_variants_semgrep,
    _build_semgrep_config,
    _check_variants,
    _load_variant_tracker,
    _save_variant_tracker,
    _dispatch_variant_missions,
    _write_variant_journal_section,
    _grep_includes_for_finding,
    _semgrep_languages_for_finding,
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

    def test_detects_unsafe_yaml_load(self):
        diff = "+data = yaml.load(raw_input)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "unsafe YAML deserialization"

    def test_detects_yaml_unsafe_load(self):
        diff = "+data = yaml.unsafe_load(content)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "unsafe YAML deserialization"

    def test_yaml_safe_loader_not_flagged(self):
        diff = "+data = yaml.load(content, Loader=yaml.SafeLoader)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 0

    def test_detects_private_key(self):
        diff = "+-----BEGIN RSA PRIVATE KEY-----"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "private key in source"

    def test_detects_generic_private_key(self):
        diff = "+-----BEGIN PRIVATE KEY-----"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "private key in source"

    def test_detects_weak_hash_md5(self):
        diff = "+digest = hashlib.md5(data)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "weak cryptographic hash"

    def test_detects_weak_hash_sha1(self):
        diff = "+h = hashlib.sha1(payload)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "weak cryptographic hash"

    def test_weak_hash_declared_non_security_is_not_flagged(self):
        diff = (
            '+h = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).hexdigest()'
        )
        findings = scan_diff_for_patterns(diff)
        assert findings == []

    def test_detects_insecure_mktemp(self):
        diff = "+path = tempfile.mktemp(suffix='.tmp')"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "insecure temp file creation"

    def test_detects_template_injection(self):
        diff = "+return render_template_string(user_input)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "potential template injection"

    def test_detects_verify_false(self):
        diff = "+requests.get(url, verify=False)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "SSL/TLS verification bypass"

    def test_detects_extractall(self):
        diff = "+tar.extractall('/tmp/output')"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "unsafe archive extraction"

    def test_detects_dynamic_import(self):
        diff = "+mod = __import__(module_name)"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "dynamic import"

    def test_detects_aws_access_key(self):
        diff = "+aws_key = '" + "AKIA" + "A" * 16 + "'"
        findings = scan_diff_for_patterns(diff)
        assert len(findings) == 1
        assert findings[0][0] == "AWS access key ID"

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
        br = {"file_count": 8, "sensitive_file_count": 2,
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

    @patch("app.post_mission_reflection.write_to_journal")
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

    @patch("app.post_mission_reflection.write_to_journal")
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

    @patch("app.post_mission_reflection.write_to_journal")
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

    @patch("app.post_mission_reflection.write_to_journal", side_effect=Exception("fail"))
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

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_disabled_returns_true(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": False}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert bool(result) is True
        mock_diff.assert_not_called()

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_no_config_returns_true(self, mock_config, mock_diff, mock_files, mock_journal):
        mock_config.return_value = None
        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert bool(result) is True

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is True

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is True

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is True  # Non-blocking mode

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is False  # Blocking mode, high risk

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is False  # Medium risk meets medium threshold

    @patch("app.post_mission_reflection.write_to_journal")
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

    @patch("app.post_mission_reflection.write_to_journal")
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
        assert bool(result) is False  # Per-project override enables blocking


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
    def test_wrapper_returns_false_on_error(self, mock_check):
        """Fail CLOSED: a crashed review must block auto-merge, not promote it."""
        from app.mission_runner import check_security_review as wrapper
        result = wrapper("/instance", "myapp", "/tmp/myapp")
        assert result is False


# ---------------------------------------------------------------------------
# projects_config accessor
# ---------------------------------------------------------------------------


class TestGetProjectSecurityReview:
    """Tests for get_project_security_review() in projects_config."""

    def test_defaults_when_not_configured(self):
        from app.projects_config import get_project_security_review
        config = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        result = get_project_security_review(config, "myapp")
        assert result == {
            "enabled": False, "blocking": False, "severity_threshold": "high",
            "variant_analysis": {"enabled": False, "max_variant_missions": 3},
        }

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
        assert result == {
            "enabled": True, "blocking": True, "severity_threshold": "medium",
            "variant_analysis": {"enabled": False, "max_variant_missions": 3},
        }

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
        assert result == {
            "enabled": False, "blocking": False, "severity_threshold": "high",
            "variant_analysis": {"enabled": False, "max_variant_missions": 3},
        }

    def test_severity_threshold_normalized(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {"severity_threshold": "  HIGH  "}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result["severity_threshold"] == "high"

    def test_defaults_include_variant_analysis(self):
        from app.projects_config import get_project_security_review
        config = {"projects": {"myapp": {"path": "/tmp/myapp"}}}
        result = get_project_security_review(config, "myapp")
        assert result["variant_analysis"] == {"enabled": False, "max_variant_missions": 3}

    def test_variant_analysis_override(self):
        from app.projects_config import get_project_security_review
        config = {
            "defaults": {"security_review": {
                "enabled": True,
                "variant_analysis": {"enabled": True, "max_variant_missions": 5},
            }},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        result = get_project_security_review(config, "myapp")
        assert result["variant_analysis"]["enabled"] is True
        assert result["variant_analysis"]["max_variant_missions"] == 5


# ---------------------------------------------------------------------------
# SecurityReviewResult
# ---------------------------------------------------------------------------


class TestSecurityReviewResult:
    """Tests for SecurityReviewResult dataclass."""

    def test_approved_is_truthy(self):
        r = SecurityReviewResult(approved=True, risk_level="low", score=0)
        assert bool(r) is True

    def test_blocked_is_falsy(self):
        r = SecurityReviewResult(approved=False, risk_level="high", score=15)
        assert bool(r) is False

    def test_default_variant_fields(self):
        r = SecurityReviewResult(approved=True, risk_level="low", score=0)
        assert r.variant_patterns == []
        assert r.variant_hits == []

    def test_variant_patterns_carried(self):
        r = SecurityReviewResult(
            approved=True, risk_level="medium", score=8,
            variant_patterns=[r"eval\s*\("],
        )
        assert len(r.variant_patterns) == 1

    def test_used_in_if_statement(self):
        r_pass = SecurityReviewResult(approved=True, risk_level="low", score=0)
        r_block = SecurityReviewResult(approved=False, risk_level="high", score=15)
        assert r_pass
        assert not r_block


# ---------------------------------------------------------------------------
# _extract_variant_patterns
# ---------------------------------------------------------------------------


class TestExtractVariantPatterns:
    """Tests for _extract_variant_patterns()."""

    def test_extracts_eval_pattern(self):
        findings = [("eval() usage", "eval(x)", "result = eval(x)")]
        patterns = _extract_variant_patterns(findings)
        assert any("eval" in p for p, _desc in patterns)

    def test_extracts_shell_true_pattern(self):
        findings = [("shell=True subprocess", "subprocess.run(cmd, shell=True)", "subprocess.run(cmd, shell=True)")]
        patterns = _extract_variant_patterns(findings)
        assert any("shell" in p.lower() for p, _desc in patterns)

    def test_empty_findings_returns_empty(self):
        assert _extract_variant_patterns([]) == []

    def test_deduplicates_patterns(self):
        findings = [
            ("eval() usage", "eval(x)", "eval(x)"),
            ("eval() usage", "eval(y)", "eval(y)"),
        ]
        patterns = _extract_variant_patterns(findings)
        assert len(patterns) == 1

    def test_multiple_distinct_patterns(self):
        findings = [
            ("eval() usage", "eval(x)", "eval(x)"),
            ("os.system() usage", "os.system('cmd')", "os.system('cmd')"),
        ]
        patterns = _extract_variant_patterns(findings)
        assert len(patterns) == 2

    def test_returns_tuples_with_description(self):
        findings = [("eval() usage", "eval(x)", "eval(x)")]
        patterns = _extract_variant_patterns(findings)
        assert len(patterns) == 1
        pattern, desc = patterns[0]
        assert "eval" in pattern
        assert desc == "eval() usage"


# ---------------------------------------------------------------------------
# _extract_diff_lines
# ---------------------------------------------------------------------------


class TestExtractDiffLines:
    """Tests for _extract_diff_lines()."""

    def test_extracts_added_lines(self):
        diff = (
            "diff --git a/src/utils.py b/src/utils.py\n"
            "--- a/src/utils.py\n"
            "+++ b/src/utils.py\n"
            "@@ -5,3 +5,4 @@\n"
            " context line\n"
            "+new line\n"
            " more context\n"
        )
        lines = _extract_diff_lines(diff)
        assert ("src/utils.py", 6) in lines

    def test_multiple_hunks(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+added1\n"
            " line3\n"
            "@@ -10,3 +11,4 @@\n"
            " line10\n"
            "+added2\n"
            " line12\n"
        )
        lines = _extract_diff_lines(diff)
        assert ("a.py", 2) in lines
        assert ("a.py", 12) in lines

    def test_empty_diff(self):
        assert _extract_diff_lines("") == set()

    def test_multiple_files(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+added_a\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+added_b\n"
        )
        lines = _extract_diff_lines(diff)
        assert ("a.py", 2) in lines
        assert ("b.py", 2) in lines


# ---------------------------------------------------------------------------
# _check_variants_grep
# ---------------------------------------------------------------------------


class TestCheckVariantsGrep:
    """Tests for _check_variants_grep()."""

    @patch("app.security_review.subprocess.run")
    def test_finds_matches(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/utils.py:10:result = eval(user_input)\nsrc/other.py:20:x = eval(data)\n",
        )
        hits = _check_variants_grep(
            [(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set(),
        )
        assert len(hits) == 2
        assert hits[0] == ("src/utils.py", 10, "result = eval(user_input)")
        assert hits[1] == ("src/other.py", 20, "x = eval(data)")

    @patch("app.security_review.subprocess.run")
    def test_excludes_diff_lines(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/utils.py:10:eval(x)\nsrc/utils.py:20:eval(y)\n",
        )
        hits = _check_variants_grep(
            [(r"eval\s*\(", "eval() usage")], "/project",
            exclude_lines={("src/utils.py", 10)},
        )
        assert len(hits) == 1
        assert hits[0][1] == 20

    @patch("app.security_review.subprocess.run")
    def test_no_matches_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        hits = _check_variants_grep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert hits == []

    @patch("app.security_review.subprocess.run")
    def test_multiple_patterns(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="a.py:1:eval(x)\n"),
            MagicMock(returncode=0, stdout="b.py:2:os.system('cmd')\n"),
        ]
        hits = _check_variants_grep(
            [(r"eval\s*\(", "eval() usage"), (r"os\.system\s*\(", "os.system() usage")],
            "/project", exclude_lines=set(),
        )
        assert len(hits) == 2

    @patch("app.security_review.subprocess.run", side_effect=FileNotFoundError)
    def test_handles_missing_grep(self, mock_run):
        hits = _check_variants_grep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert hits == []

    @patch("app.security_review.subprocess.run", side_effect=subprocess.TimeoutExpired("grep", 30))
    def test_handles_timeout(self, mock_run):
        hits = _check_variants_grep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert hits == []

    @patch("app.security_review.subprocess.run")
    def test_js_patterns_use_js_includes(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="app.tsx:5:el.innerHTML = x\n")
        _check_variants_grep(
            [(r"\.innerHTML\s*=", "potential XSS via innerHTML")],
            "/project", exclude_lines=set(),
        )
        cmd = mock_run.call_args[0][0]
        assert "--include=*.js" in cmd or "--include=*.tsx" in cmd


# ---------------------------------------------------------------------------
# _build_semgrep_config / _check_variants_semgrep
# ---------------------------------------------------------------------------


class TestBuildSemgrepConfig:
    """Tests for _build_semgrep_config()."""

    def test_single_pattern(self):
        import json
        config_str = _build_semgrep_config([(r"eval\s*\(", "eval() usage")])
        data = json.loads(config_str)
        assert "rules" in data
        assert len(data["rules"]) == 1
        assert "pattern-regex" in data["rules"][0]
        assert "eval" in data["rules"][0]["pattern-regex"]

    def test_multiple_patterns(self):
        import json
        config_str = _build_semgrep_config([
            (r"eval\s*\(", "eval() usage"),
            (r"exec\s*\(", "exec() usage"),
        ])
        data = json.loads(config_str)
        assert len(data["rules"]) == 2

    def test_empty_patterns(self):
        import json
        config_str = _build_semgrep_config([])
        data = json.loads(config_str)
        assert data["rules"] == []

    def test_preserves_regex_special_chars(self):
        import json
        config_str = _build_semgrep_config([
            (r"(?:api[_-]?key|secret[_-]?key|password)\s*=\s*['\"]", "hardcoded secret"),
        ])
        data = json.loads(config_str)
        assert r"['\"]" in data["rules"][0]["pattern-regex"]

    def test_js_pattern_uses_js_languages(self):
        import json
        config_str = _build_semgrep_config([(r"\.innerHTML\s*=", "potential XSS via innerHTML")])
        data = json.loads(config_str)
        assert "javascript" in data["rules"][0]["languages"]

    def test_python_pattern_uses_python_language(self):
        import json
        config_str = _build_semgrep_config([(r"eval\s*\(", "eval() usage")])
        data = json.loads(config_str)
        assert "python" in data["rules"][0]["languages"]


class TestCheckVariantsSemgrep:
    """Tests for _check_variants_semgrep()."""

    @patch("app.security_review.shutil.which", return_value="/usr/bin/semgrep")
    @patch("app.security_review.subprocess.run")
    def test_finds_matches(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"results":[{"path":"src/utils.py","start":{"line":10},"extra":{"lines":"result = eval(user_input)"}}]}',
        )
        hits = _check_variants_semgrep(
            [(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set(),
        )
        assert len(hits) == 1
        assert hits[0] == ("src/utils.py", 10, "result = eval(user_input)")

    @patch("app.security_review.shutil.which", return_value="/usr/bin/semgrep")
    @patch("app.security_review.subprocess.run")
    def test_excludes_diff_lines(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"results":[{"path":"src/utils.py","start":{"line":10},"extra":{"lines":"eval(x)"}},{"path":"src/utils.py","start":{"line":20},"extra":{"lines":"eval(y)"}}]}',
        )
        hits = _check_variants_semgrep(
            [(r"eval\s*\(", "eval() usage")], "/project",
            exclude_lines={("src/utils.py", 10)},
        )
        assert len(hits) == 1
        assert hits[0][1] == 20

    @patch("app.security_review.shutil.which", return_value=None)
    def test_returns_none_when_semgrep_missing(self, mock_which):
        result = _check_variants_semgrep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert result is None

    @patch("app.security_review.shutil.which", return_value="/usr/bin/semgrep")
    @patch("app.security_review.subprocess.run", side_effect=subprocess.TimeoutExpired("semgrep", 60))
    def test_returns_none_on_timeout(self, mock_run, mock_which):
        result = _check_variants_semgrep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert result is None

    @patch("app.security_review.shutil.which", return_value="/usr/bin/semgrep")
    @patch("app.security_review.subprocess.run")
    def test_returns_none_on_invalid_json(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        result = _check_variants_semgrep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert result is None

    @patch("app.security_review.shutil.which", return_value="/usr/bin/semgrep")
    @patch("app.security_review.subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = _check_variants_semgrep([(r"eval\s*\(", "eval() usage")], "/project", exclude_lines=set())
        assert result is None


# ---------------------------------------------------------------------------
# _check_variants router
# ---------------------------------------------------------------------------


class TestCheckVariantsRouter:
    """Tests for _check_variants() routing logic."""

    @patch("app.security_review._check_variants_semgrep")
    def test_prefers_semgrep_when_available(self, mock_semgrep):
        mock_semgrep.return_value = [("a.py", 1, "eval(x)")]
        patterns = [(r"eval\s*\(", "eval() usage")]
        hits = _check_variants(patterns, "/project", exclude_lines=set())
        assert len(hits) == 1
        mock_semgrep.assert_called_once()

    @patch("app.security_review._check_variants_grep")
    @patch("app.security_review._check_variants_semgrep", return_value=None)
    def test_falls_back_to_grep_when_semgrep_missing(self, mock_semgrep, mock_grep):
        mock_grep.return_value = [("a.py", 1, "eval(x)")]
        patterns = [(r"eval\s*\(", "eval() usage")]
        hits = _check_variants(patterns, "/project", exclude_lines=set())
        assert len(hits) == 1
        mock_grep.assert_called_once()

    @patch("app.security_review._check_variants_grep")
    @patch("app.security_review._check_variants_semgrep")
    def test_falls_back_to_grep_on_semgrep_failure(self, mock_semgrep, mock_grep):
        mock_semgrep.return_value = None
        mock_grep.return_value = [("a.py", 1, "eval(x)")]
        patterns = [(r"eval\s*\(", "eval() usage")]
        hits = _check_variants(patterns, "/project", exclude_lines=set())
        assert len(hits) == 1
        mock_semgrep.assert_called_once()
        mock_grep.assert_called_once()

    @patch("app.security_review._check_variants_grep")
    @patch("app.security_review._check_variants_semgrep")
    def test_semgrep_empty_is_valid(self, mock_semgrep, mock_grep):
        mock_semgrep.return_value = []
        patterns = [(r"eval\s*\(", "eval() usage")]
        hits = _check_variants(patterns, "/project", exclude_lines=set())
        assert hits == []
        mock_grep.assert_not_called()

    def test_empty_patterns_returns_empty(self):
        hits = _check_variants([], "/project", exclude_lines=set())
        assert hits == []


# ---------------------------------------------------------------------------
# Variant tracker
# ---------------------------------------------------------------------------


class TestVariantTracker:
    """Tests for variant dispatch tracker load/save."""

    def test_load_empty(self, tmp_path):
        data = _load_variant_tracker(str(tmp_path))
        assert data == {}

    def test_save_and_load(self, tmp_path):
        _save_variant_tracker(str(tmp_path), {"key1": True})
        data = _load_variant_tracker(str(tmp_path))
        assert data == {"key1": True}

    def test_load_corrupt_json(self, tmp_path):
        (tmp_path / ".variant-dispatch-tracker.json").write_text("not json")
        data = _load_variant_tracker(str(tmp_path))
        assert data == {}


# ---------------------------------------------------------------------------
# _dispatch_variant_missions
# ---------------------------------------------------------------------------


class TestDispatchVariantMissions:
    """Tests for _dispatch_variant_missions()."""

    @patch("app.security_review._save_variant_tracker")
    @patch("app.security_review._load_variant_tracker", return_value={})
    @patch("app.utils.insert_pending_mission", return_value=True)
    def test_dispatches_missions(self, mock_insert, mock_load, mock_save, tmp_path):
        hits = [
            ("src/a.py", 10, "eval(x)"),
            ("src/b.py", 20, "eval(y)"),
        ]
        dispatched = _dispatch_variant_missions(
            str(tmp_path), "myapp", hits, max_missions=3,
        )
        assert dispatched == 2
        assert mock_insert.call_count == 2

    @patch("app.security_review._save_variant_tracker")
    @patch("app.security_review._load_variant_tracker", return_value={})
    @patch("app.utils.insert_pending_mission", return_value=True)
    def test_respects_max_missions_cap(self, mock_insert, mock_load, mock_save, tmp_path):
        hits = [
            ("src/a.py", 10, "eval(x)"),
            ("src/b.py", 20, "eval(y)"),
            ("src/c.py", 30, "eval(z)"),
            ("src/d.py", 40, "eval(w)"),
        ]
        dispatched = _dispatch_variant_missions(
            str(tmp_path), "myapp", hits, max_missions=2,
        )
        assert dispatched == 2
        assert mock_insert.call_count == 2

    @patch("app.security_review._save_variant_tracker")
    @patch("app.security_review._load_variant_tracker")
    @patch("app.utils.insert_pending_mission", return_value=True)
    def test_skips_already_dispatched(self, mock_insert, mock_load, mock_save, tmp_path):
        fp = hashlib.sha256("myapp:src/a.py:10".encode()).hexdigest()
        mock_load.return_value = {f"myapp:{fp}": True}
        hits = [
            ("src/a.py", 10, "eval(x)"),
            ("src/b.py", 20, "eval(y)"),
        ]
        dispatched = _dispatch_variant_missions(
            str(tmp_path), "myapp", hits, max_missions=3,
        )
        assert dispatched == 1

    @patch("app.security_review._save_variant_tracker")
    @patch("app.security_review._load_variant_tracker", return_value={})
    @patch("app.utils.insert_pending_mission", return_value=True)
    def test_empty_hits_dispatches_nothing(self, mock_insert, mock_load, mock_save, tmp_path):
        dispatched = _dispatch_variant_missions(
            str(tmp_path), "myapp", [], max_missions=3,
        )
        assert dispatched == 0
        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# _write_variant_journal_section
# ---------------------------------------------------------------------------


class TestWriteVariantJournalSection:
    """Tests for variant journal section."""

    @patch("app.post_mission_reflection.write_to_journal")
    def test_variant_hits_logged(self, mock_write):
        _write_variant_journal_section(
            "/instance", "myapp",
            [("src/a.py", 10, "eval(x)"), ("src/b.py", 20, "eval(y)")],
        )
        mock_write.assert_called_once()
        entry = mock_write.call_args[0][1]
        assert "[VARIANT]" in entry
        assert "src/a.py" in entry
        assert "src/b.py" in entry

    @patch("app.post_mission_reflection.write_to_journal")
    def test_no_hits_skips_journal(self, mock_write):
        _write_variant_journal_section("/instance", "myapp", [])
        mock_write.assert_not_called()

    @patch("app.post_mission_reflection.write_to_journal")
    def test_truncates_many_hits(self, mock_write):
        hits = [(f"src/f{i}.py", i, f"eval(x{i})") for i in range(20)]
        _write_variant_journal_section("/instance", "myapp", hits)
        entry = mock_write.call_args[0][1]
        assert "more" in entry


# ---------------------------------------------------------------------------
# Integration: check_security_review with variants
# ---------------------------------------------------------------------------


class TestCheckSecurityReviewWithVariants:
    """Integration test: full variant pipeline through check_security_review()."""

    @patch("app.security_review._dispatch_variant_missions", return_value=1)
    @patch("app.security_review._write_variant_journal_section")
    @patch("app.security_review._check_variants")
    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_variant_pipeline_end_to_end(
        self, mock_config, mock_diff, mock_files, mock_journal,
        mock_check_variants, mock_variant_journal, mock_dispatch,
    ):
        mock_config.return_value = {
            "defaults": {
                "security_review": {
                    "enabled": True,
                    "blocking": False,
                    "variant_analysis": {"enabled": True, "max_variant_missions": 2},
                },
            },
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+result = eval(user_input)"
        mock_check_variants.return_value = [
            ("src/other.py", 42, "eval(untrusted)"),
        ]

        result = check_security_review("/instance", "myapp", "/tmp/myapp")

        assert isinstance(result, SecurityReviewResult)
        assert bool(result) is True
        assert len(result.variant_patterns) >= 1
        assert result.variant_hits == [("src/other.py", 42, "eval(untrusted)")]

        mock_check_variants.assert_called_once()
        mock_variant_journal.assert_called_once()
        mock_dispatch.assert_called_once_with(
            "/instance", "myapp",
            [("src/other.py", 42, "eval(untrusted)")],
            max_missions=2,
        )

    @patch("app.security_review._check_variants")
    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_variant_analysis_disabled_skips_scan(
        self, mock_config, mock_diff, mock_files, mock_journal,
        mock_check_variants,
    ):
        mock_config.return_value = {
            "defaults": {
                "security_review": {
                    "enabled": True,
                    "variant_analysis": {"enabled": False},
                },
            },
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+eval(x)"

        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert isinstance(result, SecurityReviewResult)
        assert result.variant_hits == []
        mock_check_variants.assert_not_called()

    @patch("app.security_review._dispatch_variant_missions", return_value=0)
    @patch("app.security_review._write_variant_journal_section")
    @patch("app.security_review._check_variants", return_value=[])
    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_no_variants_found(
        self, mock_config, mock_diff, mock_files, mock_journal,
        mock_check_variants, mock_variant_journal, mock_dispatch,
    ):
        mock_config.return_value = {
            "defaults": {
                "security_review": {
                    "enabled": True,
                    "variant_analysis": {"enabled": True},
                },
            },
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+eval(x)"

        result = check_security_review("/instance", "myapp", "/tmp/myapp")
        assert result.variant_hits == []
        mock_variant_journal.assert_not_called()
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Language-aware helpers
# ---------------------------------------------------------------------------


class TestGrepIncludesForFinding:
    """Tests for _grep_includes_for_finding()."""

    def test_python_pattern(self):
        includes = _grep_includes_for_finding("eval() usage")
        assert includes == [
            "--include=*.py", "--include=*.js", "--include=*.jsx",
            "--include=*.ts", "--include=*.tsx",
        ]

    def test_js_pattern(self):
        includes = _grep_includes_for_finding("potential XSS via innerHTML")
        assert "--include=*.js" in includes
        assert "--include=*.tsx" in includes

    def test_universal_pattern(self):
        includes = _grep_includes_for_finding("hardcoded secret")
        assert includes == [
            "--include=*.py", "--include=*.js", "--include=*.jsx",
            "--include=*.ts", "--include=*.tsx", "--include=*.rb",
            "--include=*.java", "--include=*.go", "--include=*.sh",
            "--include=*.yaml", "--include=*.yml",
        ]


class TestSemgrepLanguagesForFinding:
    """Tests for _semgrep_languages_for_finding()."""

    def test_python_pattern(self):
        langs = _semgrep_languages_for_finding("eval() usage")
        assert langs == ["python", "javascript", "typescript"]

    def test_js_pattern(self):
        langs = _semgrep_languages_for_finding("React XSS risk")
        assert "javascript" in langs
        assert "typescript" in langs

    def test_universal_pattern(self):
        langs = _semgrep_languages_for_finding("hardcoded secret")
        assert "python" in langs
        assert "javascript" in langs
        assert "ruby" in langs
        assert "java" in langs
        assert "go" in langs


# ---------------------------------------------------------------------------
# Fingerprint cross-project isolation
# ---------------------------------------------------------------------------


class TestFingerprintIsolation:
    """Tests that variant fingerprints include project_name."""

    @patch("app.security_review._save_variant_tracker")
    @patch("app.security_review._load_variant_tracker", return_value={})
    @patch("app.utils.insert_pending_mission", return_value=True)
    def test_different_projects_different_fingerprints(self, mock_insert, mock_load, mock_save, tmp_path):
        hits = [("src/a.py", 10, "eval(x)")]
        _dispatch_variant_missions(str(tmp_path), "project_a", hits, max_missions=3)
        tracker_a = mock_save.call_args[0][1]

        mock_load.return_value = {}
        mock_save.reset_mock()
        _dispatch_variant_missions(str(tmp_path), "project_b", hits, max_missions=3)
        tracker_b = mock_save.call_args[0][1]

        keys_a = set(tracker_a.keys())
        keys_b = set(tracker_b.keys())
        assert keys_a != keys_b


# ---------------------------------------------------------------------------
# Security audit log (.security-audit.jsonl) — fail-closed observability
# ---------------------------------------------------------------------------


class TestSecurityAuditLog:
    """log_security_event / count_security_blocks and review-path logging."""

    def _read_audit(self, instance_dir):
        import json
        path = Path(instance_dir) / ".security-audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_completed_review_writes_audit_entry(
        self, mock_config, mock_diff, mock_files, mock_journal, tmp_path
    ):
        """A completed (approved) review appends a structured audit entry."""
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": True, "blocking": True}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = ["src/main.py"]
        mock_diff.return_value = "+x = 1"
        check_security_review(str(tmp_path), "myapp", "/tmp/myapp")
        entries = self._read_audit(str(tmp_path))
        assert len(entries) == 1
        assert entries[0]["approved"] is True
        assert entries[0]["project"] == "myapp"
        assert entries[0]["changed_files"] == ["src/main.py"]

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_blocked_review_writes_audit_entry(
        self, mock_config, mock_diff, mock_files, mock_journal, tmp_path
    ):
        """A blocked review records approved=False plus a block_reason."""
        mock_config.return_value = {
            "defaults": {"security_review": {
                "enabled": True, "blocking": True, "severity_threshold": "high",
            }},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.return_value = [".env", "Dockerfile", "src/auth.py"] + [f"f{i}.py" for i in range(20)]
        mock_diff.return_value = "\n".join([
            "+eval(x)", "+os.system('cmd')", "+subprocess.run(x, shell=True)",
        ])
        check_security_review(str(tmp_path), "myapp", "/tmp/myapp")
        entries = self._read_audit(str(tmp_path))
        assert len(entries) == 1
        assert entries[0]["approved"] is False
        assert entries[0]["block_reason"]

    @patch("app.post_mission_reflection.write_to_journal")
    @patch("app.security_review.get_changed_files")
    @patch("app.security_review.get_diff_against_base")
    @patch("app.projects_config.load_projects_config")
    def test_crashed_review_audits_error_and_reraises(
        self, mock_config, mock_diff, mock_files, mock_journal, tmp_path
    ):
        """A crash during review records error_class and propagates the exception."""
        mock_config.return_value = {
            "defaults": {"security_review": {"enabled": True, "blocking": True}},
            "projects": {"myapp": {"path": "/tmp/myapp"}},
        }
        mock_files.side_effect = RuntimeError("git diff exploded")
        with pytest.raises(RuntimeError):
            check_security_review(str(tmp_path), "myapp", "/tmp/myapp")
        entries = self._read_audit(str(tmp_path))
        assert len(entries) == 1
        assert entries[0]["approved"] is False
        assert entries[0]["error_class"] == "RuntimeError"
        assert "exploded" in entries[0]["error_msg"]

    def test_count_security_blocks_counts_recent_blocks_only(self, tmp_path):
        from app.security_review import log_security_event, count_security_blocks

        blocked = SecurityReviewResult(approved=False, risk_level="high", score=9)
        approved = SecurityReviewResult(approved=True, risk_level="low", score=0)
        log_security_event(str(tmp_path), "p", blocked, block_reason="x")
        log_security_event(str(tmp_path), "p", blocked, block_reason="y")
        log_security_event(str(tmp_path), "p", approved)
        assert count_security_blocks(str(tmp_path), days=7) == 2

    def test_count_security_blocks_missing_file_returns_zero(self, tmp_path):
        from app.security_review import count_security_blocks
        assert count_security_blocks(str(tmp_path), days=7) == 0
