---
name: rebase
scope: core
group: pr
description: "Queue a PR rebase mission (ex: /rebase https://github.com/owner/repo/pull/42)"
version: 2.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: rebase
    description: "Queue a PR rebase (ex: /rebase https://github.com/owner/repo/pull/42)"
    aliases: [rb]
handler: handler.py
---
