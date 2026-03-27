---
name: magic
scope: core
group: ideas
description: Instant creative exploration of a project
version: 1.1.0
audience: bridge
commands:
  - name: magic
    description: Instantly explore a project and suggest ideas
    usage: |
      /magic [project]

      Picks a random project (or targets a specific one), runs a quick
      single-turn Claude call, and returns creative improvement ideas
      directly in the chat.
      Unlike /ai (deep, mission-queued), /magic is instant and lightweight.

      Examples:
        /magic          — explore a random project
        /magic koan     — explore the koan project
        /magic backend  — explore the backend project
worker: true
handler: handler.py
---
