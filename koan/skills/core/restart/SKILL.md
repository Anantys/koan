---
name: restart
scope: core
group: system
description: Restart both agent and bridge processes
version: 1.0.0
audience: bridge
worker: true
commands:
  - name: restart
    description: Restart both processes without pulling new code
    usage: "/restart -- restart agent and bridge (no code pull)"
handler: handler.py
---
