---
name: quota
scope: core
group: status
description: Check LLM quota or override remaining %
version: 1.1.0
audience: bridge
commands:
  - name: quota
    description: Live quota metrics, or override remaining % to fix drift
    usage: /quota [remaining_%]
    aliases: [q]
handler: handler.py
---
