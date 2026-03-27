---
name: delete_project
scope: core
group: config
description: Remove a project from the workspace
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: delete_project
    description: Remove a project directory and optionally its projects.yaml entry
    usage: /delete_project <project-name>
    aliases: [delete, del, deleteproject]
handler: handler.py
---
