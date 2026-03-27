---
name: idea
scope: core
group: ideas
description: Manage the ideas backlog
version: 1.0.0
audience: bridge
commands:
  - name: idea
    description: Add or manage ideas in the backlog
    usage: /idea <text>, /idea <project> <text>, /idea [project:name] <text>, /idea promote <n>, /idea delete <n>
    aliases: [buffer]
  - name: ideas
    description: List all ideas in the backlog
handler: handler.py
---
