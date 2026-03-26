---
name: audit
scope: core
group: code
description: Audit a project codebase and create GitHub issues for each finding
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: audit
    description: Audit a project for optimizations, simplifications, and issues — creates GitHub issues for findings
    usage: /audit <project-name> [extra context]
    aliases: []
handler: handler.py
worker: true
---
