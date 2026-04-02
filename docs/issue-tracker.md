# Issue Tracker Enrichment

When reviewing a PR (`/review`), Koan can automatically fetch summaries of referenced JIRA tickets or cross-repo GitHub issues and include them as context in the review prompt. This helps the reviewer understand the motivation and requirements behind the code changes.

## How It Works

1. Koan scans the PR description for issue references:
   - **JIRA**: ticket IDs like `PROJ-123`, `ABC-42` (2-10 uppercase letters, dash, digits)
   - **GitHub**: cross-repo refs like `owner/repo#123` (in-repo `#123` refs are not matched)
2. Fetches the ticket/issue summary from the appropriate backend
3. Injects the context into the review prompt under a `## Issue Tracker Context` heading
4. Each excerpt is capped at 500 characters, total output at 1000 characters

## Configuration

### GitHub (Default — Zero Config)

GitHub cross-repo issue enrichment works out of the box with no configuration needed. It uses the existing `gh` CLI authentication.

If no `issue_tracker` section exists in `config.yaml`, GitHub is used as the default.

You can also be explicit:

```yaml
# config.yaml
issue_tracker:
  type: github
```

### JIRA

JIRA integration requires credentials for the REST API (Basic auth):

```yaml
# config.yaml
issue_tracker:
  type: jira
  base_url: "https://your-org.atlassian.net"   # Your Atlassian domain
  email: "bot@your-org.com"                    # Account email for Basic auth
  api_token: "your-api-token"                  # API token (required to enable)
```

**Security note:** The `api_token` is stored in `config.yaml` inside the `instance/` directory, which is gitignored to prevent accidental commits.

For JIRA on-prem with a non-standard API version, include the version in `base_url`:

```yaml
issue_tracker:
  base_url: "https://jira.example.com/rest/api/3"
```

All three fields (`base_url`, `email`, `api_token`) must be present and non-empty for JIRA to be enabled. If any is missing, a warning is logged and the feature falls back to disabled.

## Per-Project Overrides

You can override the issue tracker type per project in `projects.yaml`. This is useful when different projects use different trackers.

```yaml
# projects.yaml
projects:
  webapp:
    path: ~/Code/webapp
    issue_tracker:
      type: jira
      base_url: "https://webapp-team.atlassian.net"
      email: "bot@webapp-team.com"
      api_token: "webapp-api-token"

  api-service:
    path: ~/Code/api
    issue_tracker: github    # String shorthand — equivalent to {type: github}

  internal-tool:
    path: ~/Code/tool
    # No issue_tracker — inherits global config (defaults to GitHub)
```

The string shorthand (`issue_tracker: github` or `issue_tracker: jira`) is supported in both `config.yaml` and `projects.yaml`. Per-project settings are shallow-merged with global defaults, so a project can override just the type while inheriting other fields.

## Failure Handling

The enrichment is fully defensive — any failure (network timeout, auth error, missing `gh` CLI, malformed response) is logged to stderr and silently skipped. A failed enrichment never aborts or delays a review.
