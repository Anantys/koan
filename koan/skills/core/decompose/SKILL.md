---
name: decompose
scope: core
group: missions
description: "Queue a mission for LLM-driven decomposition into focused sub-tasks"
version: 1.0.0
audience: human
commands:
  - name: decompose
    description: "Break a complex mission into ordered sub-tasks before execution"
    usage: "/decompose <mission description>"
    aliases: [split]
handler: handler.py
---
