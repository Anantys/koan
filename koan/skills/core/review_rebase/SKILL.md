---
name: review_rebase
scope: core
group: pr
description: "Queue a review then rebase combo for a PR (ex: /rr https://github.com/owner/repo/pull/42)"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
commands:
  - name: reviewrebase
    description: "Queue /review then /rebase for a PR — review insights feed the rebase"
    usage: "/reviewrebase <github-pr-url>"
    aliases: [rr]
handler: handler.py
---
