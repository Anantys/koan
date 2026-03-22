---
name: review
scope: core
group: code
description: "Queue a code review mission (ex: /review https://github.com/owner/repo/pull/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: review
    description: "Queue a code review for a PR or issue"
    usage: "/review <github-pr-or-issue-url> [context] [--plan-url <issue-url>] OR /review <github-repo-url> [--limit=N]"
    aliases: [rv]
handler: handler.py
---
