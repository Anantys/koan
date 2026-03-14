---
name: tech_debt
scope: core
group: code
description: Scan a project for tech debt and queue improvement missions
version: 1.0.0
audience: hybrid
commands:
  - name: tech_debt
    description: Scan a project for duplicated code, complex functions, testing gaps, and infrastructure issues
    usage: /tech_debt [project-name]
    aliases: [td, debt]
handler: handler.py
---
