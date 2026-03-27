---
name: changelog
scope: core
group: status
description: Generate a changelog from conventional commits and journal entries
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: changelog
    description: Generate changelog from recent commits and journal entries
    usage: /changelog [project] [--since=YYYY-MM-DD] [--format=md|telegram]
    aliases: [changes]
handler: handler.py
---
