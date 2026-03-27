---
name: doctor
scope: core
group: status
description: Run diagnostic self-checks on Kōan configuration and health
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: doctor
    description: Run diagnostic self-checks
    usage: /doctor [--full]
    aliases: [diag]
handler: handler.py
---
