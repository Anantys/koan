---
name: mission
scope: core
group: missions
description: Create or manage missions
version: 1.1.0
audience: bridge
commands:
  - name: mission
    description: Create a mission (queued at bottom, use --now for top)
    usage: /mission <description>, /mission --now <desc>, /mission [project:name] <desc>
    aliases: []
handler: handler.py
---
