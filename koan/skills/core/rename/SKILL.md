---
name: rename
scope: core
group: config
description: Rename a project across all configuration and instance files
version: 1.0.0
audience: bridge
commands:
  - name: rename
    description: Rename a project everywhere (projects.yaml, memory, journals, instance files)
    usage: /rename <old_name> <new_name>
    aliases: [rename_project]
handler: handler.py
---
