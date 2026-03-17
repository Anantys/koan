"""MCP config file generation for Claude Code CLI sessions.

Generates JSON config files in the format expected by ``claude --mcp-config``:

    {
      "mcpServers": {
        "<name>": {
          "command": "<cmd>",
          "args": ["..."]
        }
      }
    }

Config files are written to ``instance/.mcp-configs/`` and keyed by project
name for reuse across runs.
"""

import json
from pathlib import Path
from typing import List, Optional


def generate_mcp_config(
    mcp_entries: List[dict],
    instance_dir: str,
    project_name: str = "default",
) -> Optional[str]:
    """Generate an MCP config JSON file for Claude Code CLI.

    Args:
        mcp_entries: List of MCP server entry dicts from projects.yaml.
            Each must have at least 'name' and 'command'.
        instance_dir: Path to the instance directory.
        project_name: Project name (used as filename key).

    Returns:
        Path to the generated JSON file, or None if no valid entries.
    """
    if not mcp_entries:
        return None

    # Build the mcpServers dict
    servers = {}
    for entry in mcp_entries:
        name = entry.get("name", "")
        command = entry.get("command", "")
        if not name or not command:
            continue

        server = {"command": command}
        if entry.get("args"):
            server["args"] = entry["args"]
        if entry.get("env"):
            server["env"] = entry["env"]

        servers[name] = server

    if not servers:
        return None

    config = {"mcpServers": servers}

    # Write to instance/.mcp-configs/<project_name>.json
    config_dir = Path(instance_dir) / ".mcp-configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(project_name).name or "default"
    config_path = config_dir / f"{safe_name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    return str(config_path)
