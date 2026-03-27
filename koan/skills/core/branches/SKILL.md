---
name: branches
scope: core
group: pr
description: List koan branches and open PRs with recommended merge order and stats
version: 1.0.0
audience: bridge
commands:
  - name: branches
    description: Show koan branches + PRs with merge order recommendation
    usage: /branches [project_name]
    aliases: [br, prs]
handler: handler.py
---
