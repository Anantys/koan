---
name: restart
scope: core
group: system
emoji: 🔄
description: Restart both agent and bridge processes
version: 1.1.0
audience: bridge
worker: true
commands:
  - name: restart
    description: Restart both processes without pulling new code
    usage: "/restart [--force] -- restart agent and bridge (no code pull); --force kills the in-flight mission instead of waiting for it"
handler: handler.py
---
