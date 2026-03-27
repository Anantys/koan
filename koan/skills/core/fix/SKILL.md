---
name: fix
scope: core
group: code
description: "Fix a GitHub issue end-to-end, or batch-queue all open issues from a repo"
version: 1.1.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: fix
    description: "Queue a fix mission for a GitHub issue — understand, plan, test, implement, and submit a PR. Can also batch-queue all open issues from a repo URL."
    usage: "/fix <issue-url> [additional context] OR /fix <repo-url> [--limit=N]"
handler: handler.py
---
