---
name: status
scope: core
group: status
description: Show Kōan status, missions, and run loop health
version: 1.0.0
audience: bridge
commands:
  - name: status
    description: Quick status overview
    aliases: [st]
  - name: ping
    description: Check if run loop is alive
    aliases: []
  - name: usage
    description: Detailed quota and progress
    aliases: []
  - name: metrics
    description: Mission success rates and reliability stats
    aliases: []
handler: handler.py
---
