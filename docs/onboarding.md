# Onboarding Guide

The onboarding wizard is an interactive CLI tool that walks you through setting up Koan for the first time. It covers everything from prerequisites to your first launch.

## Quick Start

```bash
make onboard
```

Or with `--force` to restart from scratch:

```bash
make onboard ARGS="--force"
```

## What It Does

The wizard runs through 10 steps:

| Step | What it does | Files modified |
|------|-------------|----------------|
| 1. Prerequisites | Checks Python 3.10+, git, claude CLI, gh | — |
| 2. Instance init | Creates `instance/` from template and `.env` | `instance/`, `.env` |
| 3. Virtual env | Runs `make setup` to install dependencies | `.venv/` |
| 4. Messaging | Configures Telegram or Slack credentials | `.env` |
| 5. Language | Sets preferred reply language | `instance/language.json` |
| 6. Personality | Chooses agent tonality (soul preset) | `instance/soul.md` |
| 7. Projects | Registers project directories | `projects.yaml` |
| 8. GitHub | Configures gh auth and @mention support | `.env`, `instance/config.yaml` |
| 9. Deployment | Chooses terminal, Docker, or systemd | — |
| 10. Verification | Shows summary and offers to launch | — |

## Resumable

Progress is saved to `.koan-onboarding.json` after each step. If the wizard is interrupted (Ctrl-C, error, network failure), re-run `make onboard` to continue from where you left off.

The checkpoint file is deleted automatically on successful completion.

## Personality Presets

During step 6, you can choose from five personality presets:

- **Sparring partner** (default) — analytical, direct, dry humor. Challenges your thinking.
- **Mentor** — patient, pedagogic, encouraging. Guides and teaches.
- **Pragmatist** — minimal, efficient, no-nonsense. Gets things done.
- **Creative** — playful, exploratory, lateral thinking. Suggests unexpected angles.
- **Butler** — formal, polished, deferential. Professional and respectful.

Presets are stored in `instance.example/soul-presets/`. You can customize `instance/soul.md` further after setup.

## Changing Settings Later

| Setting | How to change |
|---------|--------------|
| Language | `/language` command in Telegram |
| Personality | Edit `instance/soul.md` directly |
| Projects | Edit `projects.yaml` (see `projects.example.yaml`) |
| Messaging | Edit `.env` (KOAN_TELEGRAM_TOKEN, etc.) |
| GitHub | Edit `instance/config.yaml` github section |
| Budget/schedule | Edit `instance/config.yaml` |

## Web Wizard Alternative

The CLI wizard complements the existing web-based wizard (`make install`). Both configure the same files — use whichever you prefer. The CLI wizard covers more ground (language, personality, GitHub, deployment).

### Custom Hostname / IP

By default, the web wizard binds to `127.0.0.1` (localhost only). To make it accessible from other machines on your network — for example, when running Kōan on a headless server — set `SETUP_HOSTNAME`:

```bash
SETUP_HOSTNAME=10.0.0.1 make install
```

This binds the wizard to the specified address. Use `0.0.0.0` to listen on all interfaces.

## Non-Interactive Mode

If stdin is not a TTY (e.g., in CI), the wizard uses default values for all prompts. Set `NO_COLOR=1` to disable colored output.
