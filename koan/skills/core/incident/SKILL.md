---
name: incident
scope: core
group: system
description: "Triage and fix a production error from a pasted stack trace or log snippet"
version: 1.0.0
audience: hybrid
commands:
  - name: incident
    description: "Parse a production error, identify root cause, propose a fix with tests, and submit a draft PR"
    usage: "/incident <error text or stack trace>"
handler: handler.py
---
