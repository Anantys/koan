---
name: deepplan
scope: core
group: code
description: Spec-first design with Socratic exploration of 2-3 approaches before planning
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: deepplan
    description: Deep design an idea — explores approaches, posts spec as GitHub issue, queues /plan
    usage: /deepplan <idea>, /deepplan <project> <idea>
    aliases: [deeplan]
handler: handler.py
---
