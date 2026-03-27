---
name: check
scope: core
group: code
description: Queue a check mission for a GitHub PR or Issue (rebase, review, plan)
version: 2.0.0
audience: hybrid
commands:
  - name: check
    description: Queue a check on a PR/issue (rebase, review, plan)
    usage: /check https://github.com/owner/repo/pull/123
    aliases: [inspect]
handler: handler.py
---
