---
name: ai
scope: core
group: ideas
description: Queue an AI exploration mission for a project
version: 1.0.0
audience: hybrid
commands:
  - name: ai
    description: Queue an AI exploration mission for a project
    aliases: [ia]
    usage: |
      /ai [project]
      /ia [project]

      Queues a mission that explores a project in depth via a dedicated
      CLI runner (app.ai_runner) and suggests creative improvements.
      Runs as a full agent mission with access to the codebase.

      Examples:
        /ai         — explore a random project
        /ai koan    — explore the koan project
        /ia backend — explore the backend project
handler: handler.py
---
