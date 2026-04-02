---
name: review
scope: core
group: code
emoji: 🔍
description: "Queue a code review mission (ex: /review https://github.com/owner/repo/pull/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: review
    description: "Queue a code review for a PR or issue. Flags: --architecture (SOLID/layering focus), --errors (silent-failure-hunter pass), --plan-url <issue-url> (plan alignment check)"
    usage: "/review <github-pr-or-issue-url> [context] [--architecture] [--errors] [--plan-url <issue-url>] OR /review <github-repo-url> [--limit=N]"
    aliases: [rv]
handler: handler.py
---
