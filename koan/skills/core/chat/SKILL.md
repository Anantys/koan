---
name: chat
scope: core
group: missions
description: Force chat mode (bypass mission detection)
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: chat
    description: Force chat mode for messages that look like missions
    usage: /chat <message>
    aliases: []
handler: handler.py
---
