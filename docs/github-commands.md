# GitHub Notification-Driven Commands

Control Kōan directly from GitHub PR and issue comments using `@mention` commands.

> **Introduced in**: [PR #251](https://github.com/sukria/koan/pull/251) — 10 commits, 6 new modules, 102 tests.

## Overview

Instead of switching to Telegram to tell Kōan to rebase a PR or review an issue, you can post a comment on the PR/issue itself:

```
@koan-bot rebase
```

Kōan polls GitHub notifications, detects the `@mention`, validates the command and the user's permissions, reacts with 👍 to acknowledge, and queues a mission — all without webhooks or external services.

## Quick Start

### 1. Enable the feature

In `instance/config.yaml`:

```yaml
github:
  nickname: "koan-bot"          # Your bot's GitHub username (required)
  commands_enabled: true         # Master switch
  authorized_users: ["*"]        # "*" = anyone with write access, or ["alice", "bob"]
  max_age_hours: 24              # Ignore notifications older than this (default: 24)
```

### 2. Make sure `gh` is authenticated

Kōan uses the `gh` CLI for all GitHub API calls. Verify it works:

```bash
gh auth status
gh api notifications --paginate | head
```

### 3. Post a command in a PR/issue comment

```
@koan-bot rebase
```

Kōan will:
1. React with 👍 on the comment (acknowledgment)
2. Create a pending mission: `- [project:myapp] /rebase https://github.com/owner/repo/pull/42`
3. Execute it in the next agent loop iteration

## Available Commands

| Command | Aliases | What it does | Context-aware |
|---------|---------|--------------|---------------|
| `rebase` | `rb` | Rebase a PR onto latest upstream | No |
| `recreate` | `rc` | Recreate a diverged PR from scratch | No |
| `review` | `rv` | Queue a code review for a PR or issue | No |
| `implement` | `impl` | Implement a GitHub issue | **Yes** |
| `refactor` | `rf` | Queue a refactoring mission | No |

### Context-aware commands

Some commands accept additional context after the command word. For example:

```
@koan-bot implement phase 1 only
```

This creates a mission: `/implement https://github.com/owner/repo/issues/42 phase 1 only`

Only skills with `github_context_aware: true` in their `SKILL.md` receive the extra context. For other commands, trailing text is ignored.

### Using a URL in the context

If the context contains a GitHub URL, it overrides the default subject URL from the notification:

```
@koan-bot implement https://github.com/owner/other-repo/issues/99 phase 2
```

## Configuration

### Global settings (`instance/config.yaml`)

```yaml
github:
  nickname: "koan-bot"          # Bot's GitHub @mention name (required if enabled)
  commands_enabled: false        # Master switch (default: false)
  authorized_users: ["*"]        # Allowlist: "*" for all with write access, or explicit usernames
  max_age_hours: 24              # Stale notification threshold (default: 24 hours)
```

- **`nickname`**: The GitHub username Kōan uses. Must match the account behind `GH_TOKEN`. This is the `@name` users will mention.
- **`commands_enabled`**: Feature toggle. When `false`, notification polling is completely skipped.
- **`authorized_users`**: Controls who can trigger commands. Even with `["*"]`, Kōan always verifies the user has **write access** to the repository via the GitHub API. This prevents drive-by command injection from random commenters.
- **`max_age_hours`**: Notifications older than this are silently discarded. Protects against processing a backlog of stale mentions after downtime.

#### AI reply settings

When `reply_enabled: true`, Kōan responds to non-command @mentions with AI-generated replies. Two additional settings control who can trigger replies and how often:

```yaml
github:
  reply_enabled: true
  reply_authorized_users: ["*"]    # Who can trigger AI replies (default: uses authorized_users)
  reply_rate_limit: 5              # Max replies per user per hour (default: 5, min: 1)
```

- **`reply_authorized_users`**: Separate from command `authorized_users` — allows a broader audience for read-only replies without granting command execution. `["*"]` means anyone can trigger replies (no permission check at all, unlike command wildcard which still checks GitHub write access). Omit to fall back to `authorized_users`. Set `[]` to disable replies entirely.
- **`reply_rate_limit`**: Prevents API quota abuse when replies are open broadly. Tracks per-user reply counts over a rolling 1-hour window. Default: 5, minimum: 1.

### Per-project overrides (`projects.yaml`)

Override `authorized_users` and `reply_authorized_users` for specific repositories:

```yaml
projects:
  sensitive-repo:
    path: "/path/to/sensitive-repo"
    github:
      authorized_users: ["alice", "bob"]  # Only these users, not the global wildcard
      reply_authorized_users: ["*"]       # But allow AI replies for anyone
```

This is useful when the global config allows `["*"]` but a specific repo needs tighter control for commands, or vice versa for replies.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `GH_TOKEN` | GitHub authentication for the `gh` CLI (required) |
| `GITHUB_USER` | Override bot username for API calls (optional, falls back to `github.nickname`) |

## How It Works

### Architecture

The feature spans 6 modules in `koan/app/`:

```
loop_manager.py          ← Polls during sleep cycle (throttled)
  ↓
github_notifications.py  ← Fetches & filters notifications, parses @mentions
  ↓
github_command_handler.py ← Validates commands, checks permissions, creates missions
  ↓
github_config.py         ← Reads config.yaml / projects.yaml settings
  ↓
github_skill_helpers.py  ← Shared URL extraction, project resolution, mission queuing
  ↓
skills.py                ← Skill flags: github_enabled, github_context_aware
```

### Notification processing flow

```
1. Sleep cycle tick → process_github_notifications()
2. Fetch unread notifications (reason: "mention", filtered to known repos)
3. For each notification:
   a. Skip if stale (> max_age_hours)
   b. Fetch triggering comment
   c. Skip if self-mention (bot's own comments)
   d. Check in-memory + reaction-based deduplication
   e. Parse @mention → extract (command, context)
   f. Validate command → skill must have github_enabled: true
   g. Check user permission → allowlist + GitHub write access
   h. Insert mission into missions.md (BEFORE reacting — crash-safe)
   i. React with 👍 on comment (marks as processed)
   j. Mark notification thread as read
```

### Deduplication strategy

Two-tier approach to prevent duplicate missions:

1. **In-memory set**: `_processed_comments` tracks comment IDs within a session. Fast, but lost on restart.
2. **GitHub 👍 reaction**: Persistent marker. On restart, Kōan checks if it already reacted before processing.

The mission is inserted **before** the reaction is added. If Kōan crashes between these two steps, the worst case is a duplicate mission — never a lost command.

### Polling & backoff

Notifications are checked during the agent's interruptible sleep cycle, with exponential backoff:

| Condition | Check interval |
|-----------|---------------|
| Notifications found | 60 seconds (base) |
| 1 empty check | 120 seconds |
| 2 consecutive empty | 240 seconds |
| 3+ consecutive empty | 300 seconds (cap) |

Backoff resets immediately when any notification is found. This reduces unnecessary API calls during quiet periods while maintaining fast response when activity resumes.

### Error handling

When a command fails validation (unknown command, permission denied), Kōan:
1. Posts an error reply on the GitHub comment thread (❌ with explanation)
2. Includes the list of available commands for "unknown command" errors
3. Deduplicates error replies to avoid spam

### Code block protection

`@mentions` inside code blocks are ignored:

````markdown
Here's an example:
```
@koan-bot rebase  ← This is NOT processed
```

@koan-bot rebase  ← This IS processed
````

## Adding GitHub Support to a Custom Skill

Any skill can opt into GitHub @mention triggering by adding flags to its `SKILL.md`:

```yaml
---
name: my-skill
github_enabled: true              # Allow triggering via @mentions
github_context_aware: true        # Pass extra text as context (optional)
commands:
  - name: my-command
    description: "Does something useful"
handler: handler.py
---
```

The skill's handler receives the same `SkillContext` whether triggered from Telegram or GitHub. The mission format is identical: `/my-command <url> [context]`.

See [koan/skills/README.md](../koan/skills/README.md) for the full skill authoring guide.

## Security Model

### Permission checks

Every command goes through two gates:

1. **Allowlist check**: User must be in `authorized_users` (or wildcard `*` is set)
2. **Write access verification**: Even with wildcard auth, Kōan always calls the GitHub API to verify the user has `write` or `admin` permission on the repository

This means a random person commenting `@koan-bot rebase` on a public repo will be rejected — they need actual write access, not just the ability to comment.

### Stale notification protection

Notifications older than `max_age_hours` (default: 24h) are silently discarded and marked as read. This prevents processing an accumulated backlog after extended downtime.

### Self-mention filtering

Comments posted by the bot itself are always ignored, preventing infinite loops.

### Mission-first ordering

The mission is written to `missions.md` before the 👍 reaction is added. This guarantees:
- **No lost commands**: If Kōan crashes after writing the mission but before reacting, the mission persists. On restart, it will re-process the notification but find the mission already exists.
- **At-most-once reaction**: The reaction serves as a durable "processed" marker.

## Troubleshooting

### Commands not being picked up

1. **Check feature is enabled**: `commands_enabled: true` in config.yaml
2. **Verify nickname matches**: `github.nickname` must match the GitHub account behind `GH_TOKEN`
3. **Check notification visibility**: `gh api notifications --paginate` should show the mention
4. **Check logs**: `make logs` — look for `GitHub:` log entries
5. **Verify write access**: The commenting user needs write/admin permission on the repo

### Bot reacts but doesn't execute

The 👍 means Kōan acknowledged the command and created a mission. Check:
- `instance/missions.md` — the mission should be in the Pending section
- Agent loop logs — the mission will be picked up in the next iteration

### "Unknown repository" error

The repo must be configured in `projects.yaml` with a valid `path`. Kōan resolves the notification's repository against known projects. If there's no match, it can't determine where to execute.

### Duplicate missions after restart

Expected behavior when Kōan was interrupted between mission creation and reaction. The duplicate will be harmless — the agent detects already-completed missions.

## Related

- [Skills README](../koan/skills/README.md) — Skill authoring guide with `github_enabled` flag documentation
- [Messaging: Telegram](messaging-telegram.md) — Alternative command interface via Telegram
- [Messaging: Slack](messaging-slack.md) — Alternative command interface via Slack
- [PR #251](https://github.com/sukria/koan/pull/251) — Original implementation
- [Issue #243](https://github.com/sukria/koan/issues/243) — Feature request and design plan
