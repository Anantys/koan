---
name: explore
scope: core
group: config
description: Toggle per-project exploration mode in projects.yaml
version: 1.0.0
audience: bridge
commands:
  - name: explore
    description: Enable exploration or show status
    usage: /explore [project|all|none]
    aliases: [exploration]
  - name: noexplore
    description: Disable exploration for a project
    usage: /noexplore [project]
    aliases: []
handler: handler.py
---
