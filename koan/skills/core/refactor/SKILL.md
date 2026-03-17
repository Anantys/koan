---
name: refactor
scope: core
group: code
description: "Queue a refactoring mission (ex: /refactor https://github.com/owner/repo/pull/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: refactor
    description: "Queue a refactoring mission for a PR, issue, or file"
    usage: "/refactor <github-url-or-path>"
    aliases: [rf]
handler: handler.py
---
