---
name: priority
scope: core
group: missions
description: Reorder pending missions in the queue
version: 1.0.0
audience: bridge
commands:
  - name: priority
    description: Move a pending mission to a new position
    usage: /priority <n>, /priority <n> <position>
    aliases: [prio]
handler: handler.py
---
