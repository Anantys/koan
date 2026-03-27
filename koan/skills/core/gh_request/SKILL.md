---
name: gh_request
scope: core
group: pr
description: "Handle natural-language GitHub requests — classify intent and dispatch to the right skill"
version: 1.0.0
audience: hybrid
worker: true
github_enabled: true
github_context_aware: true
commands:
  - name: gh_request
    description: "Route a natural-language GitHub request to the appropriate action (fix, rebase, review, reply, etc.)"
    usage: "/gh_request <github-url> <request text>"
handler: handler.py
---
