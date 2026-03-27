---
name: cancel
scope: core
group: missions
description: Cancel a pending mission
version: 1.0.0
audience: bridge
commands:
  - name: cancel
    description: Cancel a pending mission
    usage: /cancel <n>, /cancel <keyword>
    aliases: [remove, clear]
handler: handler.py
---
