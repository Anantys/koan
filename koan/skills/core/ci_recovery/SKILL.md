---
name: ci_recovery
scope: core
group: pr
description: Show CI recovery status for open Kōan PRs
version: 1.0.0
audience: hybrid
worker: true
github_enabled: false
commands:
  - name: ci_recovery
    description: Show CI recovery status for open Kōan PRs
    aliases: [ci_fix]
handler: handler.py
---
