---
name: doctor
scope: core
description: Run diagnostic self-checks on Kōan configuration and health
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: doctor
    description: Run diagnostic self-checks
    usage: /doctor [--full]
    aliases: [diag, checkup]
handler: handler.py
---
