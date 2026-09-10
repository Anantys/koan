"""Print client configuration without modifying external files."""

import json
import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[3]
    snippet = {
        "mcpServers": {
            "koan": {
                "command": str(repository / ".venv" / "bin" / "python"),
                "args": [str(repository / "bin" / "koan-mcp")],
                "env": {"KOAN_ROOT": str(repository)},
            }
        }
    }
    json.dump(snippet, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
