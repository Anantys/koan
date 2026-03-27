"""Koan /gha_audit skill -- scan GitHub Actions workflows for security issues."""

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Vulnerability patterns
# ---------------------------------------------------------------------------

# Dangerous contexts in ${{ }} expressions inside run: blocks.
# These can be controlled by external actors (PR authors, issue reporters).
_UNSAFE_CONTEXTS = [
    "github.event.issue.title",
    "github.event.issue.body",
    "github.event.pull_request.title",
    "github.event.pull_request.body",
    "github.event.comment.body",
    "github.event.review.body",
    "github.event.review_comment.body",
    "github.event.discussion.title",
    "github.event.discussion.body",
    "github.event.pages.*.page_name",
    "github.event.commits.*.message",
    "github.event.commits.*.author.email",
    "github.event.commits.*.author.name",
    "github.event.head_commit.message",
    "github.event.head_commit.author.email",
    "github.event.head_commit.author.name",
    "github.head_ref",
    "github.event.workflow_run.head_branch",
    "github.event.workflow_run.head_commit.message",
    "github.event.inputs.*",
]

# Build regex pattern that matches ${{ <unsafe_context> }} anywhere.
# Wildcards (*) in context paths match one dotted segment.
_UNSAFE_EXPR_PATTERNS = []
for ctx in _UNSAFE_CONTEXTS:
    escaped = re.escape(ctx).replace(r"\*", r"[^}]+")
    _UNSAFE_EXPR_PATTERNS.append(
        re.compile(r"\$\{\{\s*" + escaped + r"\s*\}\}", re.IGNORECASE)
    )

# Pwn-request triggers: workflows triggered by pull_request_target or
# issue_comment that also checkout PR code are dangerous.
_PWN_REQUEST_TRIGGERS = {"pull_request_target", "issue_comment"}

# Action reference not pinned to a full SHA (40 hex chars).
_ACTION_REF_RE = re.compile(
    r"^\s*-?\s*uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)", re.MULTILINE
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Match `run:` key to detect run block boundaries.
# In YAML steps, run: can appear as `- run:` or `  run:` (continuation).
_RUN_KEY_RE = re.compile(r"^(\s*(?:-\s+)?)run:\s*", re.MULTILINE)

# Simple trigger detection.
_ON_TRIGGER_RE = re.compile(
    r"^\s*on:\s*\n(.*?)(?=^[a-z]|\Z)", re.MULTILINE | re.DOTALL
)
_TRIGGER_EVENT_RE = re.compile(r"^\s+(\w+):", re.MULTILINE)
_ON_INLINE_RE = re.compile(r"^\s*on:\s*\[([^\]]+)\]", re.MULTILINE)
_ON_SINGLE_RE = re.compile(r"^\s*on:\s+(\w+)\s*$", re.MULTILINE)

# Permissions detection.
_PERMISSIONS_RE = re.compile(r"^\s*permissions:", re.MULTILINE)

# Checkout action detection (for pwn-request analysis).
_CHECKOUT_RE = re.compile(
    r"uses:\s*actions/checkout@\S+", re.MULTILINE
)
_CHECKOUT_PR_REF_RE = re.compile(
    r"ref:\s*\$\{\{\s*github\.event\.pull_request\.head\.", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

CRITICAL = "CRITICAL"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------

class Finding:
    """A single security finding."""

    __slots__ = ("severity", "rule", "message", "file", "line")

    def __init__(self, severity, rule, message, file, line=None):
        self.severity = severity
        self.rule = rule
        self.message = message
        self.file = file
        self.line = line

    def format(self):
        loc = f"{self.file}"
        if self.line is not None:
            loc += f":{self.line}"
        return f"[{self.severity}] {self.rule} — {self.message}\n  → {loc}"


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_workflow(filepath, content):
    """Scan a single workflow file and return a list of Findings."""
    findings = []
    name = filepath.name if isinstance(filepath, Path) else str(filepath)

    _check_expression_injection(content, name, findings)
    _check_unpinned_actions(content, name, findings)
    _check_pwn_request(content, name, findings)
    _check_missing_permissions(content, name, findings)

    return findings


def _line_number(content, pos):
    """Return 1-based line number for a character position."""
    return content[:pos].count("\n") + 1


def _check_expression_injection(content, name, findings):
    """Detect unsafe ${{ }} expressions in run: blocks."""
    lines = content.split("\n")
    in_run_block = False
    run_indent = 0

    for lineno, line in enumerate(lines, 1):
        stripped = line.lstrip()

        # Detect start of a run: block
        run_match = _RUN_KEY_RE.match(line)
        if run_match:
            in_run_block = True
            # Track indent of the step that contains `run:` so we know
            # when we leave this block (any line at same or lesser indent).
            run_indent = len(line) - len(line.lstrip())
            # Check the inline part (run: echo "...")
            inline_part = line[run_match.end():]
            # Skip block scalar indicators (|, >, |-, >-)
            if inline_part.strip() not in ("|", ">", "|-", ">-", ""):
                _scan_line_for_injections(inline_part, name, lineno, findings)
            continue

        if in_run_block:
            # Still in run block if indented deeper, or blank
            if not stripped:
                continue
            current_indent = len(line) - len(stripped)
            if current_indent > run_indent:
                _scan_line_for_injections(line, name, lineno, findings)
            else:
                in_run_block = False


def _scan_line_for_injections(text, name, lineno, findings):
    """Check a single line of text for unsafe expressions."""
    for pattern in _UNSAFE_EXPR_PATTERNS:
        for expr_match in pattern.finditer(text):
            findings.append(Finding(
                severity=CRITICAL,
                rule="expression-injection",
                message=(
                    f"Unsafe expression `{expr_match.group()}` in run: block. "
                    "Attacker-controlled input may allow command injection."
                ),
                file=name,
                line=lineno,
            ))


def _check_unpinned_actions(content, name, findings):
    """Detect actions not pinned to a full commit SHA."""
    for m in _ACTION_REF_RE.finditer(content):
        action = m.group("action")
        ref = m.group("ref")
        # Skip local actions (./path)
        if action.startswith("./"):
            continue
        # Skip docker:// references
        if action.startswith("docker://"):
            continue
        if not _SHA_RE.match(ref):
            line = _line_number(content, m.start())
            # Tags like v1, v2.3.4 are medium; branches are higher risk
            severity = MEDIUM
            findings.append(Finding(
                severity=severity,
                rule="unpinned-action",
                message=(
                    f"Action `{action}@{ref}` is not pinned to a commit SHA. "
                    "Pin to a full SHA to prevent supply chain attacks."
                ),
                file=name,
                line=line,
            ))


def _get_triggers(content):
    """Extract trigger event names from a workflow."""
    triggers = set()

    # on: [push, pull_request]
    inline = _ON_INLINE_RE.search(content)
    if inline:
        for t in inline.group(1).split(","):
            triggers.add(t.strip())
        return triggers

    # on: push
    single = _ON_SINGLE_RE.search(content)
    if single:
        triggers.add(single.group(1))
        return triggers

    # on:\n  push:\n  pull_request_target:
    block = _ON_TRIGGER_RE.search(content)
    if block:
        for m in _TRIGGER_EVENT_RE.finditer(block.group(1)):
            triggers.add(m.group(1))

    return triggers


def _check_pwn_request(content, name, findings):
    """Detect pwn-request patterns (pull_request_target + checkout PR code)."""
    triggers = _get_triggers(content)
    dangerous_triggers = triggers & _PWN_REQUEST_TRIGGERS

    if not dangerous_triggers:
        return

    # Check if there's a checkout that references PR head
    has_checkout = bool(_CHECKOUT_RE.search(content))
    has_pr_ref = bool(_CHECKOUT_PR_REF_RE.search(content))

    if has_checkout and has_pr_ref:
        for trigger in dangerous_triggers:
            findings.append(Finding(
                severity=CRITICAL,
                rule="pwn-request",
                message=(
                    f"Workflow uses `{trigger}` trigger and checks out PR code. "
                    "This allows arbitrary code execution from forked PRs."
                ),
                file=name,
            ))
    elif dangerous_triggers:
        for trigger in dangerous_triggers:
            findings.append(Finding(
                severity=HIGH,
                rule="dangerous-trigger",
                message=(
                    f"Workflow uses `{trigger}` trigger which runs with write "
                    "permissions. Ensure it does not execute untrusted code."
                ),
                file=name,
            ))


def _check_missing_permissions(content, name, findings):
    """Flag workflows missing explicit permissions block."""
    if not _PERMISSIONS_RE.search(content):
        findings.append(Finding(
            severity=LOW,
            rule="missing-permissions",
            message=(
                "Workflow does not declare explicit `permissions`. "
                "Add `permissions: {}` or specific scopes to follow least privilege."
            ),
            file=name,
        ))


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

def _resolve_project_path(project_name):
    """Resolve a project name to its filesystem path."""
    from app.utils import resolve_project_path

    return resolve_project_path(project_name)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle(ctx):
    """Handle /gha_audit command — scan workflows for security issues.

    Usage:
        /gha_audit [project-name]

    Scans .github/workflows/*.yml in the given project (or the first
    known project) for security vulnerabilities.
    """
    args = ctx.args.strip()

    if args in ("-h", "--help", "help"):
        return (
            "Usage: /gha_audit [project-name]\n\n"
            "Scans .github/workflows/*.yml for:\n"
            "• Expression injection (${{ }} in run: blocks)\n"
            "• Unpinned actions (supply chain risk)\n"
            "• Pwn-request patterns (pull_request_target + checkout)\n"
            "• Missing permissions declarations"
        )

    # Resolve project path
    project_path = None
    project_label = args if args else None

    if args:
        # Try the argument as a project name directly
        project_path = _resolve_project_path(args)
        if project_path:
            project_label = args

    if not project_path:
        # Try first known project
        from app.utils import get_known_projects

        projects = get_known_projects()
        if projects:
            first_project = next(iter(projects))
            project_path = _resolve_project_path(first_project)
            if not project_label:
                project_label = first_project

    if not project_path:
        return "\u274c No project found. Usage: /gha_audit <project-name>"

    project_dir = Path(project_path)
    workflows_dir = project_dir / ".github" / "workflows"

    if not workflows_dir.is_dir():
        return f"\u2705 No workflows directory found in `{project_label}` — nothing to audit."

    # Scan all workflow files
    workflow_files = sorted(
        list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
    )

    if not workflow_files:
        return f"\u2705 No workflow files found in `{project_label}` — nothing to audit."

    all_findings = []
    for wf in workflow_files:
        content = wf.read_text(encoding="utf-8", errors="replace")
        rel_path = wf.relative_to(project_dir)
        findings = scan_workflow(rel_path, content)
        all_findings.extend(findings)

    # Format report
    return _format_report(project_label, all_findings, len(workflow_files))


def _format_report(project, findings, file_count):
    """Format findings into a readable report."""
    if not findings:
        return (
            f"\u2705 **GHA Audit: {project}**\n\n"
            f"Scanned {file_count} workflow file(s) — no issues found."
        )

    # Group by severity
    by_severity = {}
    for f in findings:
        by_severity.setdefault(f.severity, []).append(f)

    severity_order = [CRITICAL, HIGH, MEDIUM, LOW]
    severity_emoji = {
        CRITICAL: "\U0001f534",
        HIGH: "\U0001f7e0",
        MEDIUM: "\U0001f7e1",
        LOW: "\u26aa",
    }

    lines = [
        f"\U0001f6e1 **GHA Audit: {project}**\n",
        f"Scanned {file_count} workflow file(s) — "
        f"found **{len(findings)}** issue(s).\n",
    ]

    for sev in severity_order:
        items = by_severity.get(sev, [])
        if not items:
            continue
        emoji = severity_emoji.get(sev, "")
        lines.append(f"\n{emoji} **{sev}** ({len(items)})")
        for item in items:
            lines.append(f"  {item.format()}")

    return "\n".join(lines)
