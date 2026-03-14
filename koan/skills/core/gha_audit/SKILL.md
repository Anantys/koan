---
name: gha_audit
scope: core
group: system
description: Scan GitHub Actions workflows for security vulnerabilities
version: 1.0.0
audience: bridge
commands:
  - name: gha_audit
    description: Audit GitHub Actions workflows for security issues
    usage: "/gha_audit [project-name]"
    aliases: [gha]
handler: handler.py
worker: false
---
