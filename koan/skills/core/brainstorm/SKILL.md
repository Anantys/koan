---
name: brainstorm
scope: core
group: code
description: Decompose a broad topic into linked GitHub issues with a master tracking issue
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: brainstorm
    description: Break down a topic into detailed sub-issues grouped under a master issue
    usage: /brainstorm <topic>, /brainstorm <project> <topic>, /brainstorm <topic> --tag <label>
handler: handler.py
---
