---
name: recurring
scope: core
group: missions
description: Manage recurring missions (hourly, daily, weekly, custom interval)
version: 1.2.0
audience: bridge
commands:
  - name: daily
    description: Add a daily recurring mission
    usage: /daily [HH:MM] <text> [project:<name>]
  - name: hourly
    description: Add an hourly recurring mission
    usage: /hourly <text> [project:<name>]
  - name: weekly
    description: Add a weekly recurring mission
    usage: /weekly [HH:MM] <text> [project:<name>]
  - name: every
    description: Add a custom-interval recurring mission
    usage: /every <interval> <text> [project:<name>]
  - name: recurring
    description: List all recurring missions
    usage: /recurring
  - name: cancel_recurring
    description: Cancel a recurring mission
    usage: /cancel_recurring <n>, /cancel_recurring <keyword>
handler: handler.py
---
