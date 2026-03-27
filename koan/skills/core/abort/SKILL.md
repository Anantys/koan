---
name: abort
scope: core
group: missions
description: Abort the current in-progress mission and move to the next one
version: 1.0.0
audience: bridge
commands:
  - name: abort
    description: Abort the current mission and pick up the next pending one
    usage: /abort
handler: handler.py
---
