# Operations

* [Auto-Update](auto-update.md) - Describes Kōan's opt-in auto-update feature that checks for and pulls upstream commits, plus the always-on release-tag notification.
* [Real-time Config Sync](config-sync.md) - Documents real-time config sync: the dashboard reflects config.yaml/projects.yaml edits within ~2s over the existing SSE, classifying safe hot-reload keys vs restart-required changes and gating restarts on agent idleness.
* [Web Dashboard](dashboard.md) - Documents the local Flask web dashboard's architecture, blueprints, pages, passphrase gate, and design-system integration.
* [Interactive launcher (make koan)](interactive-launcher.md) - Describes `make koan`, the TTY-gated interactive launcher and its textual terminal dashboard (tabs, toggles, keybindings).
* [make logs formatting](log-formatting.md) - Documents the display-side [cli] log formatter (log_fmt.py) behind make logs, its glyph legend, tool-input previews, accumulating thinking dots, and the raw=1 escape hatch.
* [Maintenance & Release](maint.md) - Covers Kōan's release pipeline (/koan.incubate preps, release.yml executes), branch philosophy (`main` / `incubating` / `stable` branch + `latest` tag), the ${NEXT} changelog flow into CHANGES.md, versioning scheme, and recovery steps.
* [MCP Server](mcp-server.md) - Configure Kōan's MCP server for local stdio clients or remote Streamable HTTP clients: shared bearer auth, TLS proxying, audits, and lifecycle.
* [Memory footprint: process RSS vs cgroup memory.current](memory-footprint.md) - Why the container memory graph plateaus high after missions (page cache + slab, not a leak), the /tmp leftovers that inflate it, the post-mission sweep, the per-mission cgroup scope that kills leaked build daemons, and the anon-first triage rule.
* [Memory watchdog (#2232)](memory-watchdog.md) - Explains the memory watchdog that restarts the agent loop between missions when RSS stays over a threshold, its config knobs, and health-endpoint observability.
* [Mission-queue break-glass CLI](mission-cli.md) - Terminal commands (make missions / make mission-rm, or python -m app.mission_ctl) to inspect and edit the SQLite mission store directly when the Telegram bridge is unresponsive.
* [PR Activity Reports](pr-reports.md) - Documents the `/report` skill that posts per-project and global GitHub PR activity digests (created/merged/interacted metrics) over weekly/monthly windows.
* [REST API](rest-api.md) - Documents Kōan's optional, token-authenticated HTTP control layer (missions, projects, pause/resume, config, admin, usage/metrics/logs endpoints), its generated OpenAPI spec + drift guard, and its security model.
* [RTK integration](rtk.md) - Explains the optional rtk CLI-proxy integration (detection, awareness injection, hook setup) that compresses dev-command output for token savings.
* [Skill evaluation (eval) harness](skill-evals.md) - Describes the deterministic eval harness that scores LLM-driven skills (review, fix, plan, brainstorm, rebase) against golden datasets in offline (CI) and live modes.
* [Troubleshooting](troubleshooting.md) - Catalogs common operational issues (agent loop, git/worktrees, memory, bridge, GitHub, CLI provider, parallel sessions, config) and their fixes.
