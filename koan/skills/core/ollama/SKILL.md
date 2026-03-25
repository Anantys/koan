---
name: ollama
scope: core
description: Manage Ollama models and server status
version: 1.0.0
audience: bridge
commands:
  - name: ollama
    description: Ollama model and server management
    usage: /ollama [list|pull|remove|show|status|help]
    aliases: []
group: system
handler: handler.py
worker: true
---
