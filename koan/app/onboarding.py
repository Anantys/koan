#!/usr/bin/env python3
"""
Kōan — CLI Onboarding Wizard

Interactive terminal-based setup that walks a first-time user through
every configuration step. Resumable via checkpoint file.

Usage:
    python -m app.onboarding [--force]
    make onboard
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Paths — computed from file location, not KOAN_ROOT env var
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
KOAN_ROOT = SCRIPT_DIR.parent.parent  # koan/app/.. → koan/.. → repo root
CHECKPOINT_FILE = KOAN_ROOT / ".koan-onboarding.json"

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

_use_color = (
    os.environ.get("NO_COLOR") is None
    and hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
)

_is_interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def _col(code: str, text: str) -> str:
    if not _use_color:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _col("1", text)


def green(text: str) -> str:
    return _col("32", text)


def yellow(text: str) -> str:
    return _col("33", text)


def red(text: str) -> str:
    return _col("31", text)


def dim(text: str) -> str:
    return _col("2", text)


def cyan(text: str) -> str:
    return _col("36", text)


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def ask(prompt: str, default: Optional[str] = None) -> str:
    """Prompt user for text input with optional default."""
    if not _is_interactive:
        return default or ""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default or ""
    return value if value else (default or "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer."""
    if not _is_interactive:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"  {prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not value:
        return default
    return value.startswith("y")


def ask_choice(prompt: str, options: list[str], default: int = 0) -> int:
    """Present numbered choices. Returns index of selected option."""
    if not _is_interactive:
        return default
    print()
    for i, opt in enumerate(options):
        marker = bold("→") if i == default else " "
        print(f"  {marker} {i + 1}. {opt}")
    print()
    try:
        value = input(f"  {prompt} [1-{len(options)}, default {default + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not value:
        return default
    try:
        idx = int(value) - 1
        if 0 <= idx < len(options):
            return idx
    except ValueError:
        pass
    return default


def ask_path(prompt: str, must_exist: bool = True) -> str:
    """Prompt user for a filesystem path with ~ expansion."""
    raw = ask(prompt)
    if not raw:
        return ""
    expanded = str(Path(raw).expanduser())
    if must_exist and not Path(expanded).exists():
        print(f"  {red('✗')} Path does not exist: {expanded}")
        return ""
    return expanded


def pause(message: str = "Press Enter to continue →") -> None:
    """Wait for the user to press Enter before proceeding."""
    if not _is_interactive:
        return
    try:
        input(f"\n  {dim(message)} ")
    except (EOFError, KeyboardInterrupt):
        print()


# ---------------------------------------------------------------------------
# Onboarding state
# ---------------------------------------------------------------------------


@dataclass
class OnboardingState:
    """Persistent state for the onboarding wizard."""

    completed_steps: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def mark_complete(self, step_name: str) -> None:
        if step_name not in self.completed_steps:
            self.completed_steps.append(step_name)

    def is_complete(self, step_name: str) -> bool:
        return step_name in self.completed_steps

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "completed_steps": self.completed_steps,
                    "data": self.data,
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: Path) -> "OnboardingState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            return cls(
                completed_steps=raw.get("completed_steps", []),
                data=raw.get("data", {}),
            )
        except (json.JSONDecodeError, OSError):
            return cls()


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    description: str
    run: Callable[["OnboardingState"], "OnboardingState"]
    check: Optional[Callable[["OnboardingState"], bool]] = None


def _check_tool(name: str) -> Optional[str]:
    """Return tool path if found, None otherwise."""
    return shutil.which(name)


def _run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kwargs)


# ---------------------------------------------------------------------------
# Step 1: Prerequisites
# ---------------------------------------------------------------------------


def step_prerequisites(state: OnboardingState) -> OnboardingState:
    print(f"\n  {bold('Checking prerequisites...')}\n")

    # Python version
    py_ver = platform.python_version()
    py_ok = sys.version_info >= (3, 10)
    status = green("✓") if py_ok else red("✗")
    print(f"  {status} Python {py_ver}" + ("" if py_ok else f" {red('(3.10+ required)')}"))

    # Git
    git = _check_tool("git")
    print(f"  {green('✓') if git else red('✗')} git" + (f" ({git})" if git else " (required — install git)"))

    # Claude CLI
    claude = _check_tool("claude")
    print(f"  {green('✓') if claude else red('✗')} claude CLI" + (
        f" ({claude})" if claude else f" {red('(required — https://docs.anthropic.com/en/docs/claude-code)')}"
    ))

    # gh CLI (optional)
    gh = _check_tool("gh")
    print(f"  {green('✓') if gh else yellow('○')} gh CLI" + (
        f" ({gh})" if gh else f" {dim('(optional — https://cli.github.com)')}"
    ))

    # Node/npm (optional)
    node = _check_tool("node")
    print(f"  {green('✓') if node else yellow('○')} node" + (
        f" ({node})" if node else f" {dim('(optional)')}"
    ))

    print()

    if not py_ok:
        print(f"  {red('Python 3.10 or later is required. Please upgrade.')}")
        sys.exit(1)

    if not git:
        print(f"  {red('git is required. Please install it.')}")
        sys.exit(1)

    if not claude:
        print(f"  {yellow('Claude CLI is required for the agent to work.')}")
        print(f"  {dim('You can continue setup and install it later.')}")
        print()

    state.data["has_claude"] = bool(claude)
    state.data["has_gh"] = bool(gh)

    pause()
    return state


# ---------------------------------------------------------------------------
# Step 2: Instance initialization
# ---------------------------------------------------------------------------


def _instance_dir() -> Path:
    return KOAN_ROOT / "instance"


def _env_file() -> Path:
    return KOAN_ROOT / ".env"


def step_instance_init(state: OnboardingState) -> OnboardingState:
    from app.setup_wizard import create_env_file, create_instance_dir, update_env_var

    instance_dir = _instance_dir()
    env_file = _env_file()

    if instance_dir.exists() and env_file.exists():
        print(f"  {green('✓')} Instance directory and .env already exist.")
        pause()
        return state

    print(f"  Creating instance directory and .env file...")

    if not instance_dir.exists():
        ok = create_instance_dir()
        if ok:
            print(f"  {green('✓')} Created instance/")
        else:
            print(f"  {red('✗')} Failed to create instance/ — is instance.example/ present?")
            sys.exit(1)

    if not env_file.exists():
        ok = create_env_file()
        if ok:
            print(f"  {green('✓')} Created .env")
        else:
            print(f"  {red('✗')} Failed to create .env — is env.example present?")
            sys.exit(1)

    update_env_var("KOAN_ROOT", str(KOAN_ROOT))
    print(f"  {green('✓')} Set KOAN_ROOT={KOAN_ROOT}")

    pause()
    return state


def check_instance_init(state: OnboardingState) -> bool:
    return _instance_dir().exists() and _env_file().exists()


# ---------------------------------------------------------------------------
# Step 3: Virtual environment
# ---------------------------------------------------------------------------


def step_venv(state: OnboardingState) -> OnboardingState:
    venv_marker = KOAN_ROOT / ".venv" / ".installed"
    if venv_marker.exists():
        print(f"  {green('✓')} Virtual environment already set up.")
        pause()
        return state

    print(f"  Running {bold('make setup')} to create virtual environment...")
    print(f"  {dim('(this may take a minute)')}")
    print()

    try:
        result = subprocess.run(
            ["make", "setup"],
            cwd=str(KOAN_ROOT),
            timeout=300,
        )
        if result.returncode == 0:
            print(f"\n  {green('✓')} Virtual environment ready.")
        else:
            print(f"\n  {red('✗')} make setup failed (exit code {result.returncode}).")
            print(f"  {dim('You can retry by running: make setup')}")
    except subprocess.TimeoutExpired:
        print(f"\n  {red('✗')} make setup timed out.")
    except FileNotFoundError:
        print(f"\n  {red('✗')} make not found. Run: pip install -r koan/requirements.txt")

    pause()
    return state


def check_venv(state: OnboardingState) -> bool:
    return (KOAN_ROOT / ".venv").exists()


# ---------------------------------------------------------------------------
# Step 4: Messaging configuration
# ---------------------------------------------------------------------------


def step_messaging(state: OnboardingState) -> OnboardingState:
    from app.setup_wizard import (
        get_chat_id_from_updates,
        get_env_var,
        update_env_var,
        verify_telegram_token,
    )

    # Check if already configured
    token = get_env_var("KOAN_TELEGRAM_TOKEN")
    chat_id = get_env_var("KOAN_TELEGRAM_CHAT_ID")
    if token and "your-bot-token" not in token and chat_id and "your-chat-id" not in chat_id:
        print(f"  {green('✓')} Messaging already configured.")
        return state

    provider_idx = ask_choice(
        "Which messaging platform?",
        ["Telegram (default)", "Slack"],
        default=0,
    )

    if provider_idx == 1:
        # Slack setup
        print(f"\n  {bold('Slack setup')}")
        print(f"  {dim('See docs/messaging-slack.md for setup instructions.')}")
        print()

        bot_token = ask("Slack Bot Token (xoxb-...)")
        app_token = ask("Slack App Token (xapp-...)")
        channel_id = ask("Slack Channel ID (C01234ABCD)")

        if bot_token and app_token and channel_id:
            update_env_var("KOAN_SLACK_BOT_TOKEN", bot_token)
            update_env_var("KOAN_SLACK_APP_TOKEN", app_token)
            update_env_var("KOAN_SLACK_CHANNEL_ID", channel_id)
            update_env_var("KOAN_MESSAGING_PROVIDER", "slack")
            state.data["messaging_provider"] = "slack"
            print(f"\n  {green('✓')} Slack configuration saved.")
        else:
            print(f"\n  {yellow('○')} Incomplete Slack config — skipping for now.")
    else:
        # Telegram setup
        print(f"\n  {bold('Telegram setup')}")
        print(f"  {dim('1. Open Telegram, search for @BotFather')}")
        print(f"  {dim('2. Send /newbot and follow the instructions')}")
        print(f"  {dim('3. Copy the bot token (format: 123456789:ABC-DEF1234...)')}")
        print()

        bot_token = ask("Bot token")
        if not bot_token:
            print(f"  {yellow('○')} No token provided — skipping messaging setup.")
            return state

        # Verify token
        print(f"  Verifying token...", end="", flush=True)
        result = verify_telegram_token(bot_token)
        if result.get("valid"):
            print(f" {green('✓')} Bot: @{result.get('username', '?')}")
        else:
            print(f" {red('✗')} Invalid token: {result.get('error', 'unknown error')}")
            return state

        update_env_var("KOAN_TELEGRAM_TOKEN", bot_token)

        # Try to auto-detect chat ID
        print(f"\n  {dim('Send any message to your bot on Telegram, then press Enter.')}")
        if _is_interactive:
            try:
                input(f"  {dim('Press Enter when ready...')}")
            except (EOFError, KeyboardInterrupt):
                pass

        chat_id_detected = get_chat_id_from_updates(bot_token)
        if chat_id_detected:
            print(f"  {green('✓')} Detected chat ID: {chat_id_detected}")
            update_env_var("KOAN_TELEGRAM_CHAT_ID", chat_id_detected)
        else:
            print(f"  {yellow('○')} Could not auto-detect chat ID.")
            manual_id = ask("Enter chat ID manually")
            if manual_id:
                update_env_var("KOAN_TELEGRAM_CHAT_ID", manual_id)
            else:
                print(f"  {yellow('○')} No chat ID — you can set it later in .env")
                return state

        state.data["messaging_provider"] = "telegram"
        print(f"\n  {green('✓')} Telegram configuration saved.")

    return state


def check_messaging(state: OnboardingState) -> bool:
    from app.setup_wizard import get_env_var

    # Telegram check
    token = get_env_var("KOAN_TELEGRAM_TOKEN")
    chat_id = get_env_var("KOAN_TELEGRAM_CHAT_ID")
    if token and "your-bot-token" not in token and chat_id and "your-chat-id" not in chat_id:
        return True
    # Slack check
    slack_token = get_env_var("KOAN_SLACK_BOT_TOKEN")
    if slack_token:
        return True
    return False


# ---------------------------------------------------------------------------
# Step 5: Language preference
# ---------------------------------------------------------------------------

LANGUAGES = [
    "English (default)",
    "French",
    "Spanish",
    "German",
    "Japanese",
    "Portuguese",
    "Italian",
    "Chinese",
    "Korean",
    "Dutch",
]


def step_language(state: OnboardingState) -> OnboardingState:
    idx = ask_choice("What language should Kōan reply in?", LANGUAGES, default=0)

    if idx == 0:
        print(f"  {green('✓')} Language: English (default)")
        state.data["language"] = "english"
    else:
        lang = LANGUAGES[idx].lower()
        # Set KOAN_ROOT for language_preference module
        os.environ.setdefault("KOAN_ROOT", str(KOAN_ROOT))
        from app.language_preference import set_language

        set_language(lang)
        print(f"  {green('✓')} Language set to {LANGUAGES[idx]}.")
        print(f"  {dim('Change later with /language')}")
        state.data["language"] = lang

    return state


# ---------------------------------------------------------------------------
# Step 6: Personality / soul preset
# ---------------------------------------------------------------------------

SOUL_PRESETS = {
    "sparring": {
        "label": "Sparring partner (default)",
        "desc": "Analytical, direct, dry humor — challenges your thinking",
        "file": "soul-sparring.md",
    },
    "mentor": {
        "label": "Mentor",
        "desc": "Patient, pedagogic, encouraging — guides and teaches",
        "file": "soul-mentor.md",
    },
    "pragmatist": {
        "label": "Pragmatist",
        "desc": "Minimal, efficient, no-nonsense — gets things done",
        "file": "soul-pragmatist.md",
    },
    "creative": {
        "label": "Creative",
        "desc": "Playful, exploratory, lateral thinking — suggests unexpected angles",
        "file": "soul-creative.md",
    },
    "butler": {
        "label": "Butler",
        "desc": "Formal, polished, deferential — professional and respectful",
        "file": "soul-butler.md",
    },
}

PRESET_KEYS = list(SOUL_PRESETS.keys())


def step_personality(state: OnboardingState) -> OnboardingState:
    options = [f"{p['label']} — {dim(p['desc'])}" for p in SOUL_PRESETS.values()]
    idx = ask_choice("Choose a personality for your agent:", options, default=0)
    preset_key = PRESET_KEYS[idx]
    preset = SOUL_PRESETS[preset_key]

    # Apply preset
    preset_dir = KOAN_ROOT / "instance.example" / "soul-presets"
    preset_file = preset_dir / preset["file"]
    soul_dest = _instance_dir() / "soul.md"

    if preset_file.exists():
        shutil.copy(preset_file, soul_dest)
        print(f"  {green('✓')} Personality: {preset['label']}")
    elif preset_key == "sparring":
        # Default soul.md is already the sparring partner
        default_soul = KOAN_ROOT / "instance.example" / "soul.md"
        if default_soul.exists() and not soul_dest.exists():
            shutil.copy(default_soul, soul_dest)
        print(f"  {green('✓')} Personality: {preset['label']} (default)")
    else:
        # Preset file missing — fall back to default
        print(f"  {yellow('○')} Preset file not found, using default personality.")

    # Address style
    print()
    address_idx = ask_choice(
        "How should the agent address you?",
        ['"my human" (default)', "By first name", "Boss", "Custom"],
        default=0,
    )

    address_style = "my human"
    if address_idx == 1:
        name = ask("Your first name")
        if name:
            address_style = name
    elif address_idx == 2:
        address_style = "boss"
    elif address_idx == 3:
        custom = ask("Custom address")
        if custom:
            address_style = custom

    state.data["personality"] = preset_key
    state.data["address_style"] = address_style

    # If an address style other than default was chosen, append it to soul.md
    if address_style != "my human" and soul_dest.exists():
        current = soul_dest.read_text()
        if "## Address Style" not in current:
            addition = (
                f"\n\n---\n\n## Address Style\n\n"
                f'When addressing the human directly, use "{address_style}".\n'
            )
            soul_dest.write_text(current + addition)

    print(f"  {green('✓')} Address style: {address_style}")

    return state


# ---------------------------------------------------------------------------
# Step 7: Project registration
# ---------------------------------------------------------------------------


def step_projects(state: OnboardingState) -> OnboardingState:
    projects_yaml = KOAN_ROOT / "projects.yaml"
    if projects_yaml.exists():
        print(f"  {green('✓')} projects.yaml already exists.")
        return state

    import yaml

    projects = []
    print(f"  {dim('Register at least one project for the agent to work on.')}")
    print(f"  {dim('Enter the full path to each project directory.')}")
    print()

    max_attempts = 50 if _is_interactive else 1
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        path = ask_path("Project path (or empty to finish)", must_exist=False)
        if not path:
            if not projects:
                if not _is_interactive:
                    break
                print(f"  {yellow('○')} At least one project is required.")
                continue
            break

        expanded = Path(path).expanduser()
        if not expanded.is_dir():
            print(f"  {red('✗')} Not a directory: {expanded}")
            continue

        name = expanded.name
        is_git = (expanded / ".git").exists()

        if not is_git:
            print(f"  {yellow('○')} Warning: {expanded} is not a git repository.")
            if not ask_yes_no("Add anyway?", default=False):
                continue

        projects.append({"name": name, "path": str(expanded)})
        print(f"  {green('✓')} Added: {name} ({expanded})")
        print()

        if not ask_yes_no("Add another project?", default=False):
            break

    if not projects:
        print(f"  {yellow('○')} No projects configured — you can add them later in projects.yaml")
        return state

    # Save projects.yaml
    config = {
        "defaults": {
            "git_auto_merge": {
                "enabled": False,
                "base_branch": "main",
                "strategy": "squash",
            }
        },
        "projects": {},
    }
    for p in sorted(projects, key=lambda x: x["name"].lower()):
        config["projects"][p["name"]] = {"path": p["path"]}

    header = (
        "# projects.yaml — Project configuration for Kōan\n"
        "#\n"
        "# See projects.example.yaml for full documentation.\n\n"
    )
    projects_yaml.write_text(
        header + yaml.dump(config, default_flow_style=False, sort_keys=False)
    )

    # Try to populate GitHub URLs
    os.environ.setdefault("KOAN_ROOT", str(KOAN_ROOT))
    try:
        from app.projects_config import ensure_github_urls

        msgs = ensure_github_urls(str(KOAN_ROOT))
        for m in msgs:
            print(f"  {dim(m)}")
    except (ImportError, OSError, ValueError):
        pass

    state.data["project_count"] = len(projects)
    print(f"\n  {green('✓')} Saved {len(projects)} project(s) to projects.yaml")
    return state


def check_projects(state: OnboardingState) -> bool:
    return (KOAN_ROOT / "projects.yaml").exists()


# ---------------------------------------------------------------------------
# Step 8: GitHub identity
# ---------------------------------------------------------------------------


def step_github(state: OnboardingState) -> OnboardingState:
    if not state.data.get("has_gh"):
        print(f"  {dim('gh CLI not found — skipping GitHub setup.')}")
        print(f"  {dim('Install gh from https://cli.github.com to enable GitHub features.')}")
        return state

    # Check auth status
    try:
        result = _run_cmd(["gh", "auth", "status"])
        authed = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        authed = False

    if not authed:
        print(f"  {yellow('○')} gh is not authenticated.")
        if ask_yes_no("Run gh auth login now?", default=True):
            # Interactive — inherit stdio
            subprocess.run(["gh", "auth", "login"])
            # Re-check
            try:
                result = _run_cmd(["gh", "auth", "status"])
                authed = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                authed = False
    else:
        print(f"  {green('✓')} gh is authenticated.")

    # GitHub @mentions
    if ask_yes_no("Enable Kōan to respond to GitHub @mentions?", default=False):
        # Detect nickname
        nickname = ""
        try:
            result = _run_cmd(["gh", "api", "user", "--jq", ".login"])
            if result.returncode == 0:
                nickname = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

        nickname = ask("GitHub bot nickname", default=nickname)
        auth_users_str = ask("Authorized users (comma-separated, or * for all)", default="*")

        if auth_users_str == "*":
            auth_users = ["*"]
        else:
            auth_users = [u.strip() for u in auth_users_str.split(",") if u.strip()]

        state.data["github_nickname"] = nickname
        state.data["github_authorized_users"] = auth_users
        state.data["github_commands_enabled"] = True

        # Update config.yaml
        _update_config_yaml_github(nickname, auth_users)
        print(f"  {green('✓')} GitHub @mention support configured.")
    else:
        state.data["github_commands_enabled"] = False

    # Git email
    git_email = ""
    try:
        result = _run_cmd(["git", "config", "user.email"])
        if result.returncode == 0:
            git_email = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    email = ask("Git email for Kōan's commits", default=git_email)
    if email:
        from app.setup_wizard import update_env_var

        update_env_var("KOAN_EMAIL", email)
        print(f"  {green('✓')} Git email: {email}")

    return state


def _update_config_yaml_github(nickname: str, auth_users: list[str]) -> None:
    """Update the github section in config.yaml."""
    import yaml

    config_file = _instance_dir() / "config.yaml"
    if not config_file.exists():
        return

    try:
        config = yaml.safe_load(config_file.read_text()) or {}
    except yaml.YAMLError:
        return

    config["github"] = {
        "nickname": nickname,
        "commands_enabled": True,
        "authorized_users": auth_users,
    }

    config_file.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


# ---------------------------------------------------------------------------
# Step 9: Deployment method
# ---------------------------------------------------------------------------


def step_deployment(state: OnboardingState) -> OnboardingState:
    is_linux = platform.system() == "Linux"

    options = ["Terminal — make start / make stop (default)"]
    option_keys = ["terminal"]

    options.append("Docker — docker compose up")
    option_keys.append("docker")

    if is_linux:
        options.append("Systemd — automatic service management")
        option_keys.append("systemd")

    idx = ask_choice("How do you want to run Kōan?", options, default=0)
    method = option_keys[idx]

    if method == "docker":
        docker_script = KOAN_ROOT / "setup-docker.sh"
        if docker_script.exists():
            print(f"\n  Running Docker setup...")
            subprocess.run(["bash", str(docker_script)], cwd=str(KOAN_ROOT))
        else:
            print(f"  {yellow('○')} setup-docker.sh not found.")

        print(f"\n  {dim('Start with: make docker-up')}")
        print(f"  {dim('If using Claude CLI: run make docker-auth first')}")

    elif method == "systemd":
        print(f"\n  {dim('Systemd service will be installed on first `make start`.')}")
        print(f"  {dim('Or run: make install-systemctl-service')}")

    else:
        print(f"\n  {dim('Start with: make start')}")
        print(f"  {dim('Stop with:  make stop')}")

    state.data["deployment_method"] = method
    print(f"\n  {green('✓')} Deployment method: {method}")
    return state


# ---------------------------------------------------------------------------
# Step 10: Final verification
# ---------------------------------------------------------------------------


def step_final(state: OnboardingState) -> OnboardingState:
    from app.setup_wizard import get_env_var

    print(f"\n  {bold('Configuration Summary')}")
    print(f"  {'─' * 40}")

    # Instance
    inst_ok = _instance_dir().exists()
    print(f"  Instance directory:  {green('✓') if inst_ok else red('✗')}")

    # .env
    env_ok = _env_file().exists()
    print(f"  Environment file:    {green('✓') if env_ok else red('✗')}")

    # Messaging
    provider = state.data.get("messaging_provider", "telegram")
    msg_ok = check_messaging(state)
    print(f"  Messaging ({provider}):  {green('✓') if msg_ok else yellow('○ not configured')}")

    # Language
    lang = state.data.get("language", "english")
    print(f"  Language:            {lang}")

    # Personality
    personality = state.data.get("personality", "sparring")
    preset = SOUL_PRESETS.get(personality, {})
    print(f"  Personality:         {preset.get('label', personality)}")

    # Projects
    proj_ok = check_projects(state)
    proj_count = state.data.get("project_count", "?")
    print(f"  Projects:            {green('✓') if proj_ok else yellow('○')} ({proj_count} configured)")

    # GitHub
    gh_enabled = state.data.get("github_commands_enabled", False)
    print(f"  GitHub @mentions:    {'enabled' if gh_enabled else dim('disabled')}")

    # Deployment
    deploy = state.data.get("deployment_method", "terminal")
    print(f"  Deployment:          {deploy}")

    # Claude CLI
    has_claude = state.data.get("has_claude", bool(_check_tool("claude")))
    print(f"  Claude CLI:          {green('✓') if has_claude else yellow('○ not found')}")

    print(f"  {'─' * 40}")
    print()

    # Validation
    issues = []
    if not inst_ok:
        issues.append("Instance directory missing")
    if not env_ok:
        issues.append(".env file missing")
    if not msg_ok:
        issues.append("Messaging not configured")
    if not proj_ok:
        issues.append("No projects configured")
    if not has_claude:
        issues.append("Claude CLI not installed")

    if issues:
        print(f"  {yellow('Warnings:')}")
        for issue in issues:
            print(f"    {yellow('○')} {issue}")
        print()

    # Offer to start
    if ask_yes_no("Start Kōan now?", default=False):
        print(f"\n  Starting Kōan...")
        subprocess.run(["make", "start"], cwd=str(KOAN_ROOT))
    else:
        print(f"\n  {bold('Next steps:')}")
        print(f"  {dim('1. Start Kōan:        make start')}")
        print(f"  {dim('2. Watch logs:         make logs')}")
        print(f"  {dim('3. Send a message to your bot on Telegram')}")
        print(f"  {dim('4. Try /help to see available commands')}")

    return state


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------


STEPS = [
    Step("prerequisites", "Check prerequisites", step_prerequisites),
    Step("instance_init", "Initialize instance", step_instance_init, check_instance_init),
    Step("venv", "Set up virtual environment", step_venv, check_venv),
    Step("messaging", "Configure messaging", step_messaging, check_messaging),
    Step("language", "Set language preference", step_language),
    Step("personality", "Choose agent personality", step_personality),
    Step("projects", "Register projects", step_projects, check_projects),
    Step("github", "Configure GitHub", step_github),
    Step("deployment", "Choose deployment method", step_deployment),
    Step("final", "Verify and launch", step_final),
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

BANNER = """\

  ██╗  ██╗ ██████╗  █████╗ ███╗   ██╗
  ██║ ██╔╝██╔═══██╗██╔══██╗████╗  ██║
  █████╔╝ ██║   ██║███████║██╔██╗ ██║
  ██╔═██╗ ██║   ██║██╔══██║██║╚██╗██║
  ██║  ██╗╚██████╔╝██║  ██║██║ ╚████║
  ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝

  Onboarding Wizard
"""


def run_onboarding(force: bool = False) -> None:
    """Run the interactive onboarding wizard."""
    print(bold(BANNER))

    if force and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        print(f"  {dim('Cleared previous progress (--force)')}")
        print()

    state = OnboardingState.load(CHECKPOINT_FILE)

    # Welcome page — explain what's about to happen
    if not state.completed_steps:
        print(f"  {bold('Welcome!')} This wizard will walk you through setting up Kōan.")
        print(f"  It takes about 5 minutes. Progress is saved after each step.")
        print()
        print(f"  {dim('Navigation: follow the prompts at each step.')}")
        print(f"  {dim('You can press Ctrl-C at any time to save and quit.')}")
        pause("Press Enter to begin →")
    else:
        completed = len(state.completed_steps)
        print(f"  {dim(f'Resuming from step {completed + 1} (progress loaded)')}")
        print()

    total = len(STEPS)
    for i, step in enumerate(STEPS, 1):
        # Skip if already completed (and file-based check passes too)
        already_done = state.is_complete(step.name)
        if already_done and step.check and step.check(state):
            continue
        if already_done and not step.check:
            continue

        print(f"\n{'─' * 50}")
        print(f"  {bold(f'Step {i}/{total}')} — {step.description}")
        print(f"{'─' * 50}")

        try:
            state = step.run(state)
            state.mark_complete(step.name)
            state.save(CHECKPOINT_FILE)
        except KeyboardInterrupt:
            print(f"\n\n  {yellow('Interrupted.')} Progress saved — run again to resume.")
            state.save(CHECKPOINT_FILE)
            sys.exit(130)
        except Exception as e:
            print(f"\n  {red(f'Error in step {step.name}:')} {e}")
            print(f"  {dim('Progress saved — run again to resume from this step.')}")
            state.save(CHECKPOINT_FILE)
            sys.exit(1)

    # Cleanup checkpoint on success
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()

    print(f"\n  {green(bold('Setup complete!'))}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Kōan Onboarding Wizard")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Restart onboarding from scratch",
    )
    args = parser.parse_args()

    run_onboarding(force=args.force)


if __name__ == "__main__":
    main()
