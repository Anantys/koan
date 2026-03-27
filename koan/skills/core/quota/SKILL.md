---
name: quota
scope: core
group: status
description: Check LLM quota or override used %
version: 1.1.0
audience: bridge
commands:
  - name: quota
    description: Live quota metrics, or override used % to fix drift
    usage: /quota [used_%]
    aliases: [q]
handler: handler.py
---
