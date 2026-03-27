---
name: squash
scope: core
group: pr
description: "Squash all PR commits into one clean commit (ex: /squash https://github.com/owner/repo/pull/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: squash
    description: "Squash PR commits into one (ex: /squash https://github.com/owner/repo/pull/42)"
    aliases: [sq]
handler: handler.py
---
