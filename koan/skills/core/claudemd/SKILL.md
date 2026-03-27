---
name: claudemd
scope: core
group: code
description: Refresh or create CLAUDE.md for a project based on recent architectural changes
version: 1.0.0
audience: hybrid
commands:
  - name: claudemd
    description: Refresh CLAUDE.md for a project
    usage: /claudemd <project-name>
    aliases: [claude, claude.md, claude_md]
handler: handler.py
---
