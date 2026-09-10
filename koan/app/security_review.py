"""
Kōan -- Differential security review on mission diffs.

Analyzes git diffs for security-sensitive patterns before auto-merge:
- Blast radius calculation (files changed, modules affected)
- Risk classification based on security-sensitive patterns
- Journal logging of review results

Integration point: called from mission_runner.run_post_mission()
between reflection and auto-merge.
"""

import hashlib
import json as _json
import re
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple

# Security-sensitive file patterns (glob-style)
SENSITIVE_FILE_PATTERNS = [
    "*.env*",
    "*secret*",
    "*credential*",
    "*auth*",
    "*password*",
    "*token*",
    "*config.yaml",
    "*config.yml",
    "Dockerfile*",
    "docker-compose*",
    "*requirements*.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "Makefile",
    "*.sql",
    "*.pem",
    "*.key",
]

# Security-sensitive content patterns (regex)
SENSITIVE_CONTENT_PATTERNS = [
    (r"(?i)\beval\s*\(", "eval() usage"),
    (r"(?i)\bexec\s*\(", "exec() usage"),
    (r"(?i)subprocess\.(?:call|run|Popen)\s*\(.*shell\s*=\s*True", "shell=True subprocess"),
    (r"(?i)os\.system\s*\(", "os.system() usage"),
    (r"(?i)SQL.*(?:format|%s|\+)", "potential SQL injection"),
    (r"(?i)(?:api[_-]?key|secret[_-]?key|password)\s*=\s*['\"]", "hardcoded secret"),
    (r"(?i)disable.*(?:ssl|tls|verify|cert)", "SSL/TLS verification disabled"),
    (r"(?i)chmod\s+(?:777|666)", "overly permissive file permissions"),
    (r"(?i)--no-verify", "verification bypass"),
    (r"(?i)CORS.*\*|Access-Control-Allow-Origin.*\*", "wildcard CORS"),
    (r"(?i)(?:pickle|marshal)\.loads?\s*\(", "unsafe deserialization"),
    (r"(?i)\.innerHTML\s*=", "potential XSS via innerHTML"),
    (r"(?i)dangerouslySetInnerHTML", "React XSS risk"),
    (r"(?i)yaml\.(?:unsafe_load\s*\(|load\s*\((?!.*SafeLoader))", "unsafe YAML deserialization"),
    (r"BEGIN[ \t]+(?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY", "private key in source"),
    # Mirrors bandit B324: a call that declares usedforsecurity=False is a
    # non-cryptographic use (checksums, dedup keys) and is not flagged. The
    # lookahead is bounded to *this* call's own argument list (one level of
    # nesting, so `hashlib.sha1(x.encode("utf-8"), usedforsecurity=False)`
    # still matches) — an unbounded `.*` would let the literal appear anywhere
    # later on the line, so a trailing comment or a second annotated call would
    # silently exempt a genuinely security-relevant hash.
    (
        r"(?i)hashlib\.(?:md5|sha1)\s*\("
        r"(?!(?:[^()]|\([^()]*\))*usedforsecurity\s*=\s*False)",
        "weak cryptographic hash",
    ),
    (r"(?i)tempfile\.mktemp\s*\(", "insecure temp file creation"),
    (r"(?i)render_template_string\s*\(", "potential template injection"),
    (r"(?i)\bverify\s*=\s*False\b", "SSL/TLS verification bypass"),
    (r"(?i)\.extractall\s*\(", "unsafe archive extraction"),
    (r"(?i)__import__\s*\(", "dynamic import"),
    (r"AKIA[A-Z0-9]{16}", "AWS access key ID"),
]

# Risk level thresholds (cumulative score → risk)
RISK_THRESHOLDS = {
    "critical": 20,
    "high": 12,
    "medium": 6,
    "low": 0,
}

# Severity ordering for threshold comparison
SEVERITY_ORDER = ["low", "medium", "high", "critical"]


@dataclass
class SecurityReviewResult:
    """Result of a differential security review.

    Bool-compatible: existing call sites that check truthiness
    continue to work via __bool__ returning self.approved.
    """
    approved: bool
    risk_level: str
    score: int
    variant_patterns: list = field(default_factory=list)
    variant_hits: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.approved


_VARIANT_PATTERN_MAP = {
    "eval() usage": r"eval\s*\(",
    "exec() usage": r"exec\s*\(",
    "shell=True subprocess": r"subprocess\.(?:call|run|Popen)\s*\(.*shell\s*=\s*True",
    "os.system() usage": r"os\.system\s*\(",
    "potential SQL injection": r"SQL.*(?:format|%s|\+)",
    "hardcoded secret": r"(?:api[_-]?key|secret[_-]?key|password)\s*=\s*['\"]",
    "SSL/TLS verification disabled": r"disable.*(?:ssl|tls|verify|cert)",
    "overly permissive file permissions": r"chmod\s+(?:777|666)",
    "verification bypass": r"--no-verify",
    "wildcard CORS": r"(?:CORS.*\*|Access-Control-Allow-Origin.*\*)",
    "unsafe deserialization": r"(?:pickle|marshal)\.loads?\s*\(",
    "potential XSS via innerHTML": r"\.innerHTML\s*=",
    "React XSS risk": r"dangerouslySetInnerHTML",
}

_SECRET_REDACT_RE = re.compile(
    r"""(?:api[_-]?key|secret[_-]?key|password|token)\s*=\s*['"][^'"]*['"]""",
    re.IGNORECASE,
)


def _redact_snippet(snippet: str, max_len: int = 80) -> str:
    redacted = _SECRET_REDACT_RE.sub("<redacted>", snippet)
    return redacted[:max_len]


_JS_PATTERNS = {"potential XSS via innerHTML", "React XSS risk", "wildcard CORS"}
_PY_AND_JS_PATTERNS = {"eval() usage", "exec() usage"}
_UNIVERSAL_PATTERNS = {
    "hardcoded secret", "SSL/TLS verification disabled",
    "overly permissive file permissions", "verification bypass",
    "potential SQL injection",
}

_GREP_INCLUDES_BY_FINDING = {
    "py": ["--include=*.py"],
    "js": ["--include=*.js", "--include=*.jsx", "--include=*.ts", "--include=*.tsx"],
    "py_js": [
        "--include=*.py", "--include=*.js", "--include=*.jsx",
        "--include=*.ts", "--include=*.tsx",
    ],
    "all": [
        "--include=*.py", "--include=*.js", "--include=*.jsx",
        "--include=*.ts", "--include=*.tsx", "--include=*.rb",
        "--include=*.java", "--include=*.go", "--include=*.sh",
        "--include=*.yaml", "--include=*.yml",
    ],
}


def _grep_includes_for_finding(description: str) -> list:
    """Return grep --include flags appropriate for finding type."""
    if description in _JS_PATTERNS:
        return list(_GREP_INCLUDES_BY_FINDING["js"])
    if description in _PY_AND_JS_PATTERNS:
        return list(_GREP_INCLUDES_BY_FINDING["py_js"])
    if description in _UNIVERSAL_PATTERNS:
        return list(_GREP_INCLUDES_BY_FINDING["all"])
    return list(_GREP_INCLUDES_BY_FINDING["py"])


_SEMGREP_LANGUAGES_BY_FINDING = {
    "py": ["python"],
    "js": ["javascript", "typescript"],
    "py_js": ["python", "javascript", "typescript"],
    "all": ["python", "javascript", "typescript", "ruby", "java", "go"],
}


def _semgrep_languages_for_finding(description: str) -> list:
    """Return semgrep language list appropriate for finding type."""
    if description in _JS_PATTERNS:
        return list(_SEMGREP_LANGUAGES_BY_FINDING["js"])
    if description in _PY_AND_JS_PATTERNS:
        return list(_SEMGREP_LANGUAGES_BY_FINDING["py_js"])
    if description in _UNIVERSAL_PATTERNS:
        return list(_SEMGREP_LANGUAGES_BY_FINDING["all"])
    return list(_SEMGREP_LANGUAGES_BY_FINDING["py"])


def _extract_variant_patterns(
    findings: List[Tuple[str, str, str]],
) -> list:
    """Extract deduplicated (pattern, description) pairs from content findings."""
    seen = set()
    patterns = []
    for description, _match, _context in findings:
        if description in seen:
            continue
        seen.add(description)
        pattern = _VARIANT_PATTERN_MAP.get(description)
        if pattern:
            patterns.append((pattern, description))
    return patterns


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")


def _extract_diff_lines(diff_text: str) -> set:
    """Extract (filepath, line_number) pairs for all added lines in a unified diff."""
    result = set()
    current_file = None
    current_line = 0

    for line in diff_text.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue

        if current_file is None:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            result.add((current_file, current_line))
            current_line += 1
        elif line.startswith("-"):
            pass
        else:
            current_line += 1

    return result


_GREP_EXCLUDES = [
    "--exclude-dir=.git",
    "--exclude-dir=node_modules",
    "--exclude-dir=.venv",
    "--exclude-dir=venv",
    "--exclude-dir=__pycache__",
    "--exclude-dir=dist",
    "--exclude-dir=build",
    "--exclude-dir=vendor",
    "--exclude-dir=.tox",
    "--exclude=*.min.js",
    "--exclude=*.map",
]


def _check_variants_grep(
    patterns: list,
    project_path: str,
    *,
    exclude_lines: set,
    deadline: float = 0,
) -> List[Tuple[str, int, str]]:
    """Scan project for variant occurrences using grep."""
    hits = []
    for pattern, description in patterns:
        if deadline and time.monotonic() > deadline:
            print(
                "[security_review] variant scan deadline reached, returning partial results",
                file=sys.stderr,
            )
            break
        includes = _grep_includes_for_finding(description)
        try:
            result = subprocess.run(
                ["grep", "-rn", "-E", *_GREP_EXCLUDES, *includes, "--", pattern, "."],
                capture_output=True, text=True,
                cwd=project_path, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 1:
                continue
            if result.returncode != 0:
                print(
                    f"[security_review] grep error (rc={result.returncode}) "
                    f"for pattern '{description}': {(result.stderr or '')[:200]}",
                    file=sys.stderr,
                )
                continue
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                filepath = parts[0].lstrip("./")
                try:
                    lineno = int(parts[1])
                except ValueError:
                    continue
                snippet = parts[2].strip()
                if (filepath, lineno) not in exclude_lines:
                    hits.append((filepath, lineno, snippet))
        except subprocess.TimeoutExpired:
            print(
                f"[security_review] grep timed out for pattern '{description}'",
                file=sys.stderr,
            )
            continue
        except (FileNotFoundError, OSError) as exc:
            print(
                f"[security_review] grep error for pattern '{description}': {exc}",
                file=sys.stderr,
            )
            continue
    return hits


def _build_semgrep_config(patterns: list) -> str:
    """Build a semgrep JSON config from (pattern, description) pairs."""
    rules = []
    for i, (pattern, description) in enumerate(patterns):
        langs = _semgrep_languages_for_finding(description)
        rules.append({
            "id": f"variant-{i}",
            "pattern-regex": pattern,
            "message": "Variant of security finding",
            "languages": langs,
            "severity": "WARNING",
        })
    return _json.dumps({"rules": rules})


def _check_variants_semgrep(
    patterns: list,
    project_path: str,
    *,
    exclude_lines: set,
) -> Optional[List[Tuple[str, int, str]]]:
    """Scan project for variant occurrences using semgrep.

    Returns None on failure (caller should fall back to grep).
    Returns empty list when semgrep succeeds but finds nothing.
    """
    if not shutil.which("semgrep"):
        return None

    json_content = _build_semgrep_config(patterns)
    hits = []
    try:
        from app.utils import koan_tmp_dir
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=koan_tmp_dir(), delete=True,
        ) as f:
            f.write(json_content)
            f.flush()
            result = subprocess.run(
                ["semgrep", "--config", f.name, "--json", "--quiet", "."],
                capture_output=True, text=True,
                cwd=project_path, timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                stderr_sample = (result.stderr or "")[:200]
                print(
                    f"[security_review] semgrep failed (rc={result.returncode})"
                    f"{': ' + stderr_sample if stderr_sample else ''}, falling back to grep",
                    file=sys.stderr,
                )
                return None
            try:
                data = _json.loads(result.stdout)
            except (ValueError, _json.JSONDecodeError):
                print(
                    "[security_review] semgrep returned invalid JSON, falling back to grep",
                    file=sys.stderr,
                )
                return None
            results = data.get("results")
            if results is None:
                print(
                    "[security_review] semgrep JSON missing 'results' key, falling back to grep",
                    file=sys.stderr,
                )
                return None
            for item in results:
                filepath = item.get("path", "")
                lineno = item.get("start", {}).get("line", 0)
                snippet = item.get("extra", {}).get("lines", "").strip()
                if (filepath, lineno) not in exclude_lines:
                    hits.append((filepath, lineno, snippet))
    except Exception as exc:
        print(
            f"[security_review] semgrep error: {exc}, falling back to grep",
            file=sys.stderr,
        )
        return None
    return hits


_VARIANT_SCAN_TIMEOUT = 90


def _check_variants(
    patterns: list,
    project_path: str,
    *,
    exclude_lines: set,
) -> List[Tuple[str, int, str]]:
    """Scan project for variant occurrences of security patterns.

    Prefers semgrep when available; falls back to grep on failure or absence.
    Global deadline caps total scan time to _VARIANT_SCAN_TIMEOUT seconds.
    """
    if not patterns:
        return []

    deadline = time.monotonic() + _VARIANT_SCAN_TIMEOUT

    result = _check_variants_semgrep(
        patterns, project_path, exclude_lines=exclude_lines,
    )
    if result is not None:
        return result
    return _check_variants_grep(
        patterns, project_path, exclude_lines=exclude_lines,
        deadline=deadline,
    )


def _variant_tracker_path(instance_dir: str) -> Path:
    return Path(instance_dir) / ".variant-dispatch-tracker.json"


def _load_variant_tracker(instance_dir: str) -> dict:
    path = _variant_tracker_path(instance_dir)
    if not path.exists():
        return {}
    try:
        return _json.loads(path.read_text())
    except (_json.JSONDecodeError, OSError) as exc:
        print(
            f"[security_review] Corrupt variant tracker {path}: {exc}",
            file=sys.stderr,
        )
        backup = path.with_suffix(".json.corrupt")
        try:
            path.rename(backup)
            print(
                f"[security_review] Preserved corrupt tracker as {backup}",
                file=sys.stderr,
            )
        except OSError:
            pass
        return {}


def _save_variant_tracker(instance_dir: str, data: dict) -> None:
    from app.utils import atomic_write_json
    atomic_write_json(_variant_tracker_path(instance_dir), data)


def _dispatch_variant_missions(
    instance_dir: str,
    project_name: str,
    hits: List[Tuple[str, int, str]],
    *,
    max_missions: int = 3,
) -> int:
    """Dispatch investigation missions for variant hits.

    Returns the number of missions actually dispatched.
    """
    if not hits:
        return 0

    tracker = _load_variant_tracker(instance_dir)
    missions_path = Path(instance_dir) / "missions.md"
    dispatched = 0

    for filepath, lineno, snippet in hits:
        if dispatched >= max_missions:
            break

        fingerprint = hashlib.sha256(
            f"{project_name}:{filepath}:{lineno}".encode()
        ).hexdigest()
        tracker_key = f"{project_name}:{fingerprint}"
        if tracker_key in tracker:
            continue

        safe_snippet = _redact_snippet(snippet)
        mission_text = (
            f"- [security-variant] Investigate security pattern variant "
            f"in `{filepath}` line {lineno}: `{safe_snippet}` "
            f"[project:{project_name}]"
        )
        from app.utils import insert_pending_mission
        inserted = insert_pending_mission(missions_path, mission_text)
        if inserted:
            tracker[tracker_key] = True
            dispatched += 1
        else:
            print(
                f"[security_review] Failed to insert variant mission for {filepath}:{lineno}",
                file=sys.stderr,
            )

    if dispatched > 0:
        _save_variant_tracker(instance_dir, tracker)

    return dispatched


def _write_variant_journal_section(
    instance_dir: str,
    project_name: str,
    hits: List[Tuple[str, int, str]],
) -> None:
    """Append a [VARIANT] section to the journal for variant hits."""
    if not hits:
        return

    try:
        from app.post_mission_reflection import write_to_journal

        lines = [f"## [VARIANT] Security variant scan — {len(hits)} hit(s)"]
        for filepath, lineno, snippet in hits[:10]:
            lines.append(f"- `{filepath}:{lineno}`: `{_redact_snippet(snippet)}`")
        if len(hits) > 10:
            lines.append(f"- ... and {len(hits) - 10} more")

        write_to_journal(Path(instance_dir), "\n".join(lines))
    except Exception as e:
        print(f"[security_review] Variant journal write failed: {e}", file=sys.stderr)


def _run_git(project_path: str, *args: str, timeout: int = 30) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True,
            cwd=project_path, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def get_diff_against_base(project_path: str, base_branch: str = "main") -> str:
    """Get unified diff of current branch against base branch.

    Tries upstream/<base>, origin/<base>, then <base> as fallbacks.
    """
    for ref in [f"upstream/{base_branch}", f"origin/{base_branch}", base_branch]:
        diff = _run_git(project_path, "diff", f"{ref}...HEAD")
        if diff:
            return diff
    return ""


def get_changed_files(project_path: str, base_branch: str = "main") -> List[str]:
    """Get list of files changed relative to base branch."""
    for ref in [f"upstream/{base_branch}", f"origin/{base_branch}", base_branch]:
        output = _run_git(project_path, "diff", "--name-only", f"{ref}...HEAD")
        if output:
            return [f for f in output.splitlines() if f.strip()]
    return []


def classify_file_sensitivity(filepath: str) -> bool:
    """Check if a file path matches any security-sensitive pattern."""
    basename = Path(filepath).name
    for pattern in SENSITIVE_FILE_PATTERNS:
        if fnmatch(basename, pattern) or fnmatch(filepath, pattern):
            return True
    return False


def scan_diff_for_patterns(diff_text: str) -> List[Tuple[str, str, str]]:
    """Scan a unified diff for security-sensitive content patterns.

    Only scans added lines (lines starting with '+', excluding '+++' headers).

    Returns:
        List of (pattern_description, matched_text, line) tuples.
    """
    findings = []
    for line in diff_text.splitlines():
        # Only scan added lines
        if not line.startswith("+") or line.startswith("+++"):
            continue

        content = line[1:]  # Strip the leading '+'
        for pattern_re, description in SENSITIVE_CONTENT_PATTERNS:
            match = re.search(pattern_re, content)
            if match:
                findings.append((description, match.group(0), content.strip()))
    return findings


def calculate_blast_radius(changed_files: List[str]) -> dict:
    """Calculate the blast radius of changes.

    Returns:
        Dict with keys: file_count, sensitive_files, sensitive_file_count,
        modules_affected, has_infra_changes, has_dependency_changes.
    """
    sensitive = [f for f in changed_files if classify_file_sensitivity(f)]

    # Count distinct top-level directories as "modules"
    modules = set()
    for f in changed_files:
        parts = Path(f).parts
        if len(parts) > 1:
            modules.add(parts[0])

    infra_patterns = ["Dockerfile*", "docker-compose*", "Makefile", "*.yml", "*.yaml"]
    has_infra = any(
        any(fnmatch(Path(f).name, p) for p in infra_patterns)
        for f in changed_files
    )

    dep_patterns = ["*requirements*.txt", "pyproject.toml", "package.json",
                    "package-lock.json", "Cargo.toml", "go.mod", "go.sum"]
    has_deps = any(
        any(fnmatch(Path(f).name, p) for p in dep_patterns)
        for f in changed_files
    )

    return {
        "file_count": len(changed_files),
        "sensitive_files": sensitive,
        "sensitive_file_count": len(sensitive),
        "modules_affected": sorted(modules),
        "has_infra_changes": has_infra,
        "has_dependency_changes": has_deps,
    }


def assess_risk_level(
    blast_radius: dict,
    content_findings: List[Tuple[str, str, str]],
) -> Tuple[str, int]:
    """Assess overall risk level from blast radius and content findings.

    Returns:
        (risk_level, score) where risk_level is one of:
        "low", "medium", "high", "critical".
    """
    score = 0

    # Blast radius scoring
    file_count = blast_radius.get("file_count", 0)
    if file_count > 20:
        score += 4
    elif file_count > 10:
        score += 2
    elif file_count > 5:
        score += 1

    score += blast_radius.get("sensitive_file_count", 0) * 3

    if blast_radius.get("has_infra_changes"):
        score += 3
    if blast_radius.get("has_dependency_changes"):
        score += 2

    module_count = len(blast_radius.get("modules_affected", []))
    if module_count > 3:
        score += 2
    elif module_count > 1:
        score += 1

    # Content findings scoring
    score += len(content_findings) * 2

    # Map score to risk level
    risk = "low"
    for level in ["critical", "high", "medium"]:
        if score >= RISK_THRESHOLDS[level]:
            risk = level
            break

    return risk, score


def _severity_meets_threshold(risk_level: str, threshold: str) -> bool:
    """Check if a risk level meets or exceeds a severity threshold."""
    risk_idx = SEVERITY_ORDER.index(risk_level) if risk_level in SEVERITY_ORDER else 0
    thresh_idx = SEVERITY_ORDER.index(threshold) if threshold in SEVERITY_ORDER else 2
    return risk_idx >= thresh_idx


def _write_journal_entry(
    instance_dir: str,
    project_name: str,
    risk_level: str,
    score: int,
    blast_radius: dict,
    content_findings: List[Tuple[str, str, str]],
    blocked: bool,
) -> None:
    """Write security review results to the project journal."""
    try:
        from app.post_mission_reflection import write_to_journal

        lines = [f"## Security Review — risk: {risk_level} (score: {score})"]

        br = blast_radius
        lines.append(
            f"- Files: {br['file_count']}, "
            f"Sensitive: {br['sensitive_file_count']}, "
            f"Modules: {len(br.get('modules_affected', []))}"
        )

        if br.get("has_infra_changes"):
            lines.append("- ⚠ Infrastructure changes detected")
        if br.get("has_dependency_changes"):
            lines.append("- ⚠ Dependency changes detected")

        if content_findings:
            lines.append(f"- Content findings ({len(content_findings)}):")
            # Show up to 10 findings to avoid journal bloat
            for desc, _match, context in content_findings[:10]:
                lines.append(f"  - {desc}: `{context[:80]}`")
            if len(content_findings) > 10:
                lines.append(f"  - ... and {len(content_findings) - 10} more")

        if blocked:
            lines.append("- **Auto-merge blocked** by security review")

        entry = "\n".join(lines)
        write_to_journal(instance_dir, entry)
    except Exception as e:
        print(f"[security_review] Journal write failed: {e}", file=sys.stderr)


_AUDIT_LOG_NAME = ".security-audit.jsonl"


def log_security_event(
    instance_dir: str,
    project_name: str,
    result: Optional["SecurityReviewResult"],
    *,
    changed_files: Optional[List[str]] = None,
    block_reason: str = "",
    error_class: str = "",
    error_msg: str = "",
) -> None:
    """Append a structured security-review outcome to ``.security-audit.jsonl``.

    Written on every completed review (approved or blocked) and on the
    exception path so a crashed review is auditable rather than invisible.
    Best-effort: a write failure is logged to stderr and swallowed.
    """
    from datetime import datetime

    changed = changed_files or []
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "project": project_name,
        "risk_level": result.risk_level if result else "unknown",
        "score": result.score if result else 0,
        "approved": bool(result.approved) if result else False,
        "block_reason": block_reason,
        "variant_count": len(result.variant_hits) if result else 0,
        "changed_files": changed,
    }
    if error_class:
        entry["error_class"] = error_class
        entry["error_msg"] = error_msg[:500]
    try:
        from app.locked_file import locked_jsonl_append
        locked_jsonl_append(Path(instance_dir) / _AUDIT_LOG_NAME, entry)
    except OSError as e:
        print(f"[security_review] Audit log write failed: {e}", file=sys.stderr)


def count_security_blocks(instance_dir: str, days: int = 7) -> int:
    """Count blocked (approved=False) security reviews in the last ``days``.

    Reads ``.security-audit.jsonl``. Returns 0 when the file is absent or
    unreadable — observability must never fail the request.
    """
    from datetime import datetime, timedelta

    path = Path(instance_dir) / _AUDIT_LOG_NAME
    if not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    count = 0
    try:
        from app.locked_file import locked_jsonl_read
        for line in locked_jsonl_read(path):
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except ValueError:
                continue
            if rec.get("approved", True):
                continue
            ts = rec.get("ts", "")
            try:
                when = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                # Undated entry: count it rather than silently drop a block.
                count += 1
                continue
            if when >= cutoff:
                count += 1
    except OSError:
        return 0
    return count


def check_security_review(
    instance_dir: str,
    project_name: str,
    project_path: str,
) -> SecurityReviewResult:
    """Run differential security review on the current branch.

    Analyzes the diff for security-sensitive patterns and blast radius.
    Configured via security_review section in projects.yaml.

    Returns:
        SecurityReviewResult — bool-compatible (True = proceed, False = blocked).
    """
    import os
    from app.projects_config import load_projects_config, get_project_security_review

    koan_root = os.environ.get("KOAN_ROOT", str(Path(instance_dir).parent))
    config = load_projects_config(koan_root)
    if not config:
        return SecurityReviewResult(approved=True, risk_level="low", score=0)

    sr_config = get_project_security_review(config, project_name)
    if not sr_config.get("enabled"):
        return SecurityReviewResult(approved=True, risk_level="low", score=0)

    # Get the base branch for diff comparison
    from app.projects_config import get_project_auto_merge
    merge_config = get_project_auto_merge(config, project_name)
    base_branch = merge_config.get("base_branch", "main")

    changed_files: List[str] = []
    try:
        return _run_review(
            instance_dir, project_name, project_path, base_branch,
            sr_config, lambda files: changed_files.extend(files),
        )
    except Exception as e:
        # Audit the crash so a failed review is distinguishable from a passed
        # one. Re-raise: the caller (mission_runner) fails closed on exception.
        log_security_event(
            instance_dir, project_name, None,
            changed_files=changed_files,
            block_reason="review error",
            error_class=type(e).__name__,
            error_msg=str(e),
        )
        raise


def _run_review(
    instance_dir: str,
    project_name: str,
    project_path: str,
    base_branch: str,
    sr_config: dict,
    record_files,
) -> SecurityReviewResult:
    """Core review logic. Wrapped by ``check_security_review`` for audit."""
    # Gather data
    changed_files = get_changed_files(project_path, base_branch)
    record_files(changed_files)
    if not changed_files:
        return SecurityReviewResult(approved=True, risk_level="low", score=0)

    diff_text = get_diff_against_base(project_path, base_branch)
    content_findings = scan_diff_for_patterns(diff_text) if diff_text else []
    blast_radius = calculate_blast_radius(changed_files)

    # Assess risk
    risk_level, score = assess_risk_level(blast_radius, content_findings)

    # Determine if this should block auto-merge
    threshold = sr_config.get("severity_threshold", "high")
    blocking = sr_config.get("blocking", False)
    should_block = blocking and _severity_meets_threshold(risk_level, threshold)

    # Extract variant patterns from findings
    variant_patterns = _extract_variant_patterns(content_findings)

    # Log to journal
    _write_journal_entry(
        instance_dir, project_name,
        risk_level, score, blast_radius, content_findings,
        blocked=should_block,
    )

    # Variant analysis (when enabled and there are patterns)
    variant_hits = []
    va_config = sr_config.get("variant_analysis", {})
    if not isinstance(va_config, dict):
        va_config = {}
    if va_config.get("enabled") and variant_patterns and diff_text:
        exclude_lines = _extract_diff_lines(diff_text)
        variant_hits = _check_variants(
            variant_patterns, project_path, exclude_lines=exclude_lines,
        )

        if variant_hits:
            max_missions = va_config.get("max_variant_missions", 3)
            _write_variant_journal_section(instance_dir, project_name, variant_hits)
            dispatched = _dispatch_variant_missions(
                instance_dir, project_name, variant_hits,
                max_missions=max_missions,
            )
            if dispatched != len(variant_hits):
                print(
                    f"[security_review] {dispatched}/{len(variant_hits)} "
                    f"variant missions dispatched (cap/dedup filtered remainder)",
                    file=sys.stderr,
                )

    if should_block:
        print(
            f"[security_review] Blocking auto-merge: "
            f"risk={risk_level} score={score} threshold={threshold}",
        )
        blocked_result = SecurityReviewResult(
            approved=False, risk_level=risk_level, score=score,
            variant_patterns=variant_patterns,
            variant_hits=variant_hits,
        )
        log_security_event(
            instance_dir, project_name, blocked_result,
            changed_files=changed_files,
            block_reason=f"risk={risk_level} score={score} >= {threshold}",
        )
        return blocked_result

    if risk_level in ("high", "critical"):
        print(
            f"[security_review] Warning: "
            f"risk={risk_level} score={score} (non-blocking)",
        )

    approved_result = SecurityReviewResult(
        approved=True, risk_level=risk_level, score=score,
        variant_patterns=variant_patterns,
        variant_hits=variant_hits,
    )
    log_security_event(
        instance_dir, project_name, approved_result,
        changed_files=changed_files,
    )
    return approved_result
