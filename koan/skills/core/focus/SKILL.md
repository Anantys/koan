---
name: focus
scope: core
group: config
description: Focus mode — suppress reflection and free exploration, process missions only
version: 1.0.0
audience: bridge
commands:
  - name: focus
    description: Activate focus mode (missions only, no reflection/exploration)
    usage: /focus [duration] — default 5h. Examples: /focus, /focus 3h, /focus 2h30m
    aliases: []
  - name: unfocus
    description: Deactivate focus mode
    aliases: []
handler: handler.py
---
