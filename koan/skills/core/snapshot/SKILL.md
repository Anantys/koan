---
name: snapshot
scope: core
group: status
description: Export memory state to a portable snapshot file
version: 1.0.0
audience: bridge
commands:
  - name: snapshot
    description: Export memory snapshot for backup or migration
    aliases: []
handler: handler.py
worker: true
---
