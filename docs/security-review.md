# Security Review

Kōan can automatically scan mission diffs for security-sensitive patterns before auto-merge. This provides a lightweight safety net that catches common dangerous code patterns without requiring an external tool.

## Overview

When enabled, the security review runs as part of the post-mission pipeline, between reflection and auto-merge. It:

1. **Calculates blast radius** — files changed, modules affected, infrastructure/dependency changes
2. **Scans content patterns** — eval/exec, shell injection, hardcoded secrets, unsafe deserialization, XSS, wildcard CORS, and more
3. **Classifies risk** — low / medium / high / critical based on cumulative score
4. **Logs to journal** — all findings are recorded in the project's daily journal
5. **Optionally blocks auto-merge** — when configured in blocking mode with a severity threshold

The review is designed to be fail-open: if it encounters an error (git failure, config issue), auto-merge proceeds normally.

## Configuration

Security review is configured per-project in `projects.yaml`. See `projects.example.yaml` for a full annotated example.

### Basic setup

```yaml
defaults:
  security_review:
    enabled: true              # Scan diffs for dangerous patterns
    blocking: false            # Log findings but don't block auto-merge
    severity_threshold: high   # Threshold for blocking (when blocking: true)
```

### Blocking mode

When `blocking: true`, auto-merge is skipped if the risk level meets or exceeds `severity_threshold`:

```yaml
defaults:
  security_review:
    enabled: true
    blocking: true             # Block auto-merge on risky changes
    severity_threshold: medium # Block on medium, high, or critical risk
```

### Per-project overrides

Override the defaults for specific projects:

```yaml
projects:
  production-api:
    security_review:
      enabled: true
      blocking: true           # Strict: block on risky changes
      severity_threshold: medium

  internal-tool:
    security_review:
      enabled: false           # Skip review for low-risk internal tools
```

### Options

| Setting | Default | Description |
|---|---|---|
| `enabled` | `false` | Run the security review on every mission. |
| `blocking` | `false` | Block auto-merge when risk meets the threshold. When false, findings are logged but auto-merge proceeds. |
| `severity_threshold` | `high` | Minimum risk level that triggers a block (when `blocking: true`). One of: `low`, `medium`, `high`, `critical`. |

## What It Detects

### Content patterns (added lines only)

The review scans only added lines in the diff (`+` lines), ignoring removed code:

- **`eval()` / `exec()`** — dynamic code execution
- **`subprocess` with `shell=True`** — shell injection risk
- **`os.system()`** — shell command execution
- **SQL string formatting** — potential SQL injection
- **Hardcoded secrets** — `api_key = "..."`, `password = "..."`
- **SSL/TLS verification disabled** — `disable_ssl`, `verify=False`
- **Overly permissive permissions** — `chmod 777`, `chmod 666`
- **Verification bypass** — `--no-verify` flags
- **Wildcard CORS** — `Access-Control-Allow-Origin: *`
- **Unsafe deserialization** — `pickle.load()`, `marshal.load()`
- **XSS vectors** — `.innerHTML =`, `dangerouslySetInnerHTML`

### Blast radius factors

- Number of files changed (>5, >10, >20 files increase risk)
- Sensitive file paths (secrets, credentials, auth, tokens, configs)
- Infrastructure files (Dockerfile, docker-compose, Makefile)
- Dependency files (requirements.txt, package.json, pyproject.toml, etc.)
- Number of top-level modules affected

## Risk Scoring

The risk level is calculated from a cumulative score:

| Risk Level | Score Threshold |
|---|---|
| Low | 0+ |
| Medium | 6+ |
| High | 12+ |
| Critical | 20+ |

Points are awarded for:
- File count: 1 (>5 files), 2 (>10), 4 (>20)
- Each sensitive file: 3 points
- Infrastructure changes: 3 points
- Dependency changes: 2 points
- Multiple modules: 1 (>1 module), 2 (>3 modules)
- Each content finding: 2 points

## Journal Output

Review results are written to the project's daily journal (`instance/journal/YYYY-MM-DD/project.md`):

```markdown
## Security Review — risk: medium (score: 8)
- Files: 7, Sensitive: 1, Modules: 2
- ⚠ Dependency changes detected
- Content findings (2):
  - eval() usage: `result = eval(user_input)`
  - hardcoded secret: `api_key = "sk-live-..."`
- **Auto-merge blocked** by security review
```

## Pipeline Integration

The security review runs in the post-mission pipeline in `mission_runner.py`:

1. Verification (quality gate, lint)
2. Reflection
3. **Security review** ← here
4. Auto-merge (skipped if security review blocks)

If the review itself fails (exception), it logs the error and returns "pass" to avoid blocking the pipeline on review infrastructure issues.
