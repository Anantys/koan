---
name: babysit
scope: core
group: pr
description: Monitor open PRs created by Kōan and auto-queue fixes for CI failures, review comments, and merge conflicts
version: 1.0.0
audience: hybrid
worker: true
commands:
  - name: babysit
    description: "Show babysit status, or toggle on/off"
    usage: "/babysit [on|off|<pr-url>]"
    aliases: []
handler: handler.py
---
