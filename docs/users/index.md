# Users

* [Kōan REST CLI](koan-cli.md) - Configure and use bin/koan-cli to call every operation in Kōan's token-authenticated REST API.
* [KOAN.md — koan-only project instructions](koan-md.md) - Documents the optional project-root KOAN.md file and the .koan/ directory (a second .koan/KOAN.md, per-skill .koan/skills/<skill>/*.md hooks, and a structured .koan/config.yaml with review.always_check): koan-only steering injected into the autonomous agent's system prompt but never loaded by interactive Claude Code sessions, with precedence rules, the 16k-char cap, and this repo's dogfood layout.
* [Model Configuration](model-configuration.md) - Explains how to configure which model handles each Koan role (mission, chat, lightweight, fallback, etc.) per provider via `config.yaml`, including resolution order and CLI-provider-per-role routing.
* [Onboarding Guide](onboarding.md) - Documents the interactive 12-step onboarding wizard that sets up a new Koan instance, its resumability, personality presets, and non-interactive/CI mode.
* [Kōan Quickstart — Zero to Hero](quickstart.md) - A 5-minute guide to the commands for driving Koan from GitHub PRs/issues, Jira, and messaging apps (Telegram/Slack), with minimal and context-augmented examples for each.
* [Skills Reference](skills.md) - Complete reference for all Koan slash commands (mission management, code/PR operations, scheduling, status, configuration, and system commands) usable via Telegram, Slack, or GitHub @mentions.
* [Kōan User Manual](user-manual.md) - A tiered (beginner/intermediate/power-user) walkthrough of everything Kōan can do, from queuing your first mission through parallel sessions, deep exploration, and full configuration.
