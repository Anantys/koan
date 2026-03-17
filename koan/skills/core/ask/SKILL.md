---
name: ask
scope: core
group: pr
description: "Ask Kōan a question about a GitHub PR or issue — fetches context and posts an AI reply"
version: 1.0.0
audience: hybrid
github_enabled: true
github_context_aware: true
worker: true
commands:
  - name: ask
    description: "Ask a question about a PR or issue and get an AI reply posted to GitHub"
    usage: "/ask <github-comment-url>"
handler: handler.py
---
