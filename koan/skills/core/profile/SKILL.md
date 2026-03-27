---
name: profile
scope: core
group: code
description: Queue a performance profiling mission for a managed project
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: profile
    description: Queue a performance profiling mission
    usage: /profile <project-name-or-pr-url>
    aliases: [perf, benchmark]
handler: handler.py
---
