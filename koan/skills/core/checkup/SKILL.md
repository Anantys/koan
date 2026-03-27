---
name: checkup
group: code
description: Run a health check on all open PRs across projects
commands:
  - name: checkup
    usage: /checkup
    aliases: [checkprs]
handler: handler.py
worker: true
---
