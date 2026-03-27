"""Tests for the /gha_audit skill — GitHub Actions security auditor."""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import SkillContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path, args="", command_name="gha_audit"):
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    return SkillContext(
        koan_root=tmp_path,
        instance_dir=instance_dir,
        command_name=command_name,
        args=args,
    )


def _make_workflow_dir(tmp_path, project_path, workflows=None):
    """Create a project with .github/workflows/ and optional workflow files."""
    wf_dir = Path(project_path) / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    if workflows:
        for name, content in workflows.items():
            (wf_dir / name).write_text(content)
    return wf_dir


# ---------------------------------------------------------------------------
# Scanner unit tests — expression injection
# ---------------------------------------------------------------------------


class TestExpressionInjection:
    def test_detects_issue_title_in_run(self):
        from skills.core.gha_audit.handler import scan_workflow, CRITICAL

        content = textwrap.dedent("""\
            name: test
            on: issues
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "${{ github.event.issue.title }}"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        assert len(findings) >= 1
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1
        assert expr_findings[0].severity == CRITICAL
        assert "issue.title" in expr_findings[0].message

    def test_detects_pr_body_in_run(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: pull_request
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      echo "${{ github.event.pull_request.body }}"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1
        assert "pull_request.body" in expr_findings[0].message

    def test_detects_comment_body(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: issue_comment
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo ${{ github.event.comment.body }}
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1

    def test_detects_head_ref(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: pull_request
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: git checkout ${{ github.head_ref }}
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1

    def test_detects_commit_message(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "${{ github.event.head_commit.message }}"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1

    def test_no_false_positive_for_safe_context(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "${{ github.sha }}"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 0

    def test_expression_not_in_run_block_not_flagged(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: ${{ github.event.issue.title }}
            on: issues
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 0

    def test_multiple_unsafe_expressions(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: issues
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      echo "${{ github.event.issue.title }}"
                      echo "${{ github.event.issue.body }}"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 2

    def test_line_number_reported(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = "line1\nline2\nline3\n    run: echo ${{ github.event.issue.title }}\nline5\n"
        findings = scan_workflow(Path("ci.yml"), content)
        expr_findings = [f for f in findings if f.rule == "expression-injection"]
        assert len(expr_findings) == 1
        assert expr_findings[0].line == 4


# ---------------------------------------------------------------------------
# Scanner unit tests — unpinned actions
# ---------------------------------------------------------------------------


class TestUnpinnedActions:
    def test_detects_tag_reference(self):
        from skills.core.gha_audit.handler import scan_workflow, MEDIUM

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        unpinned = [f for f in findings if f.rule == "unpinned-action"]
        assert len(unpinned) == 1
        assert unpinned[0].severity == MEDIUM
        assert "actions/checkout@v4" in unpinned[0].message

    def test_sha_reference_is_ok(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        unpinned = [f for f in findings if f.rule == "unpinned-action"]
        assert len(unpinned) == 0

    def test_multiple_actions_mixed(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: actions/setup-node@a5ac7e51b41094c92402da3b24376905380afc29
                  - uses: actions/upload-artifact@main
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        unpinned = [f for f in findings if f.rule == "unpinned-action"]
        assert len(unpinned) == 2

    def test_local_action_not_flagged(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: ./.github/actions/my-action
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        unpinned = [f for f in findings if f.rule == "unpinned-action"]
        # Local actions use paths, not @ref — regex won't match
        assert len(unpinned) == 0


# ---------------------------------------------------------------------------
# Scanner unit tests — pwn-request
# ---------------------------------------------------------------------------


class TestPwnRequest:
    def test_detects_pull_request_target_with_checkout(self):
        from skills.core.gha_audit.handler import scan_workflow, CRITICAL

        content = textwrap.dedent("""\
            name: test
            on:
              pull_request_target:
                types: [opened]
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29
                    with:
                      ref: ${{ github.event.pull_request.head.sha }}
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        pwn = [f for f in findings if f.rule == "pwn-request"]
        assert len(pwn) == 1
        assert pwn[0].severity == CRITICAL

    def test_pull_request_target_without_checkout_is_high(self):
        from skills.core.gha_audit.handler import scan_workflow, HIGH

        content = textwrap.dedent("""\
            name: test
            on:
              pull_request_target:
                types: [opened]
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "hello"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        trigger = [f for f in findings if f.rule == "dangerous-trigger"]
        assert len(trigger) == 1
        assert trigger[0].severity == HIGH

    def test_regular_pull_request_not_flagged(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on:
              pull_request:
                types: [opened]
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        pwn = [f for f in findings if f.rule in ("pwn-request", "dangerous-trigger")]
        assert len(pwn) == 0

    def test_issue_comment_trigger_flagged(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on:
              issue_comment:
                types: [created]
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "hello"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        trigger = [f for f in findings if f.rule == "dangerous-trigger"]
        assert len(trigger) == 1


# ---------------------------------------------------------------------------
# Scanner unit tests — missing permissions
# ---------------------------------------------------------------------------


class TestMissingPermissions:
    def test_flags_missing_permissions(self):
        from skills.core.gha_audit.handler import scan_workflow, LOW

        content = textwrap.dedent("""\
            name: test
            on: push
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "hello"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        perms = [f for f in findings if f.rule == "missing-permissions"]
        assert len(perms) == 1
        assert perms[0].severity == LOW

    def test_no_flag_when_permissions_present(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions:
              contents: read
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "hello"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        perms = [f for f in findings if f.rule == "missing-permissions"]
        assert len(perms) == 0

    def test_empty_permissions_block_ok(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: test
            on: push
            permissions: {}
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "hello"
        """)
        findings = scan_workflow(Path("ci.yml"), content)
        perms = [f for f in findings if f.rule == "missing-permissions"]
        assert len(perms) == 0


# ---------------------------------------------------------------------------
# Scanner unit tests — trigger detection
# ---------------------------------------------------------------------------


class TestTriggerDetection:
    def test_inline_triggers(self):
        from skills.core.gha_audit.handler import _get_triggers

        content = "on: [push, pull_request]\n"
        assert _get_triggers(content) == {"push", "pull_request"}

    def test_single_trigger(self):
        from skills.core.gha_audit.handler import _get_triggers

        content = "on: push\n"
        assert _get_triggers(content) == {"push"}

    def test_block_triggers(self):
        from skills.core.gha_audit.handler import _get_triggers

        content = textwrap.dedent("""\
            on:
              push:
                branches: [main]
              pull_request_target:
                types: [opened]
        """)
        triggers = _get_triggers(content)
        assert "push" in triggers
        assert "pull_request_target" in triggers


# ---------------------------------------------------------------------------
# Finding format
# ---------------------------------------------------------------------------


class TestFinding:
    def test_format_with_line(self):
        from skills.core.gha_audit.handler import Finding, CRITICAL

        f = Finding(CRITICAL, "test-rule", "Bad thing", "ci.yml", line=42)
        text = f.format()
        assert "[CRITICAL]" in text
        assert "test-rule" in text
        assert "Bad thing" in text
        assert "ci.yml:42" in text

    def test_format_without_line(self):
        from skills.core.gha_audit.handler import Finding, LOW

        f = Finding(LOW, "test-rule", "Minor thing", "ci.yml")
        text = f.format()
        assert "ci.yml" in text
        assert ":" not in text.split("→")[-1].strip() or "ci.yml" in text


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_clean_report(self):
        from skills.core.gha_audit.handler import _format_report

        result = _format_report("my-project", [], 3)
        assert "no issues" in result.lower()
        assert "3" in result
        assert "my-project" in result

    def test_report_with_findings(self):
        from skills.core.gha_audit.handler import _format_report, Finding, CRITICAL, LOW

        findings = [
            Finding(CRITICAL, "rule1", "Bad", "ci.yml", 10),
            Finding(LOW, "rule2", "Minor", "ci.yml"),
        ]
        result = _format_report("my-project", findings, 1)
        assert "2" in result
        assert "CRITICAL" in result
        assert "LOW" in result
        assert "my-project" in result


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------


class TestHandler:
    def test_help_flag(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        ctx = _make_ctx(tmp_path, args="--help")
        result = handle(ctx)
        assert "Usage:" in result
        assert "Expression injection" in result or "expression" in result.lower()

    def test_help_word(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        ctx = _make_ctx(tmp_path, args="help")
        result = handle(ctx)
        assert "Usage:" in result

    def test_no_project_found(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=None):
            with patch("app.utils.get_known_projects", return_value={}):
                ctx = _make_ctx(tmp_path, args="nonexistent")
                result = handle(ctx)

        assert "No project found" in result

    def test_no_workflows_dir(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        # No .github/workflows/

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            ctx = _make_ctx(tmp_path, args="my-project")
            result = handle(ctx)

        assert "No workflows directory" in result

    def test_empty_workflows_dir(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "my-project"
        _make_workflow_dir(tmp_path, project_dir)

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            ctx = _make_ctx(tmp_path, args="my-project")
            result = handle(ctx)

        assert "No workflow files" in result

    def test_clean_project(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "my-project"
        _make_workflow_dir(tmp_path, project_dir, {
            "ci.yml": textwrap.dedent("""\
                name: CI
                on: push
                permissions:
                  contents: read
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29
                      - run: echo "safe"
            """),
        })

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            ctx = _make_ctx(tmp_path, args="my-project")
            result = handle(ctx)

        assert "no issues" in result.lower()

    def test_project_with_issues(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "my-project"
        _make_workflow_dir(tmp_path, project_dir, {
            "ci.yml": textwrap.dedent("""\
                name: CI
                on: issues
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                      - run: echo "${{ github.event.issue.title }}"
            """),
        })

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            ctx = _make_ctx(tmp_path, args="my-project")
            result = handle(ctx)

        assert "CRITICAL" in result
        assert "issue(s)" in result or "issue" in result.lower()

    def test_scans_yaml_extension_too(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "my-project"
        _make_workflow_dir(tmp_path, project_dir, {
            "deploy.yaml": textwrap.dedent("""\
                name: Deploy
                on: push
                permissions: {}
                jobs:
                  deploy:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
            """),
        })

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            ctx = _make_ctx(tmp_path, args="my-project")
            result = handle(ctx)

        # Should find unpinned action
        assert "unpinned-action" in result or "MEDIUM" in result

    def test_default_project_fallback(self, tmp_path):
        from skills.core.gha_audit.handler import handle

        project_dir = tmp_path / "fallback-project"
        _make_workflow_dir(tmp_path, project_dir, {
            "ci.yml": textwrap.dedent("""\
                name: CI
                on: push
                permissions: {}
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@a5ac7e51b41094c92402da3b24376905380afc29
                      - run: echo "safe"
            """),
        })

        with patch("skills.core.gha_audit.handler._resolve_project_path",
                    return_value=str(project_dir)):
            with patch("app.utils.get_known_projects",
                        return_value={"fallback-project": str(project_dir)}):
                ctx = _make_ctx(tmp_path, args="")
                result = handle(ctx)

        assert "no issues" in result.lower()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_workflow_with_all_issues(self):
        from skills.core.gha_audit.handler import scan_workflow

        content = textwrap.dedent("""\
            name: Vulnerable
            on:
              pull_request_target:
                types: [opened]
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                    with:
                      ref: ${{ github.event.pull_request.head.sha }}
                  - run: echo "${{ github.event.pull_request.title }}"
        """)
        findings = scan_workflow(Path("vuln.yml"), content)
        rules = {f.rule for f in findings}
        assert "expression-injection" in rules
        assert "unpinned-action" in rules
        assert "pwn-request" in rules
        assert "missing-permissions" in rules

    def test_empty_workflow_file(self):
        from skills.core.gha_audit.handler import scan_workflow, Finding

        findings = scan_workflow(Path("empty.yml"), "")
        # Should not crash, may have missing-permissions at most
        assert all(isinstance(f, Finding) for f in findings)

    def test_malformed_yaml_does_not_crash(self):
        from skills.core.gha_audit.handler import scan_workflow, Finding

        content = "{{{{ invalid yaml !@#$%\n  garbage: [[[["
        findings = scan_workflow(Path("bad.yml"), content)
        assert isinstance(findings, list)
