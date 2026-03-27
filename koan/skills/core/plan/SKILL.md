---
name: plan
scope: core
group: code
description: Deep-think an idea and create a GitHub issue with a structured plan
version: 2.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: plan
    description: Plan an idea or iterate on an existing GitHub issue
    usage: /plan <idea>, /plan <project> <idea>, /plan <issue-url>
handler: handler.py
---
