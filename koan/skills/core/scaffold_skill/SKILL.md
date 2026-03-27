---
name: scaffold_skill
scope: core
description: Generate a new skill from a description
version: 1.0.0
audience: bridge
group: system
worker: true
commands:
  - name: scaffold_skill
    description: Generate SKILL.md + handler.py for a new custom skill
    usage: /scaffold_skill <scope> <name> <description>
    aliases: [scaffold, new_skill]
handler: handler.py
---
