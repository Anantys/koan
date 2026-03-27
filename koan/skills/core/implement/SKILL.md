---
name: implement
scope: core
group: code
description: "Implement a GitHub issue (ex: /implement https://github.com/owner/repo/issues/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: implement
    description: "Queue an implementation mission for a GitHub issue"
    usage: "/implement <issue-url> [additional context]"
    aliases: [impl]
handler: handler.py
---
