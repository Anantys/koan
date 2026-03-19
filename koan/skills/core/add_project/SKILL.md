---
name: add_project
scope: core
group: config
description: Add a project from a GitHub URL
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: add_project
    description: Clone a GitHub repo and add it to the workspace
    usage: /add_project <github-url> [name]
    aliases: [add, addproject]
handler: handler.py
---
