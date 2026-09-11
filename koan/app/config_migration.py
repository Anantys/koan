"""One-shot, idempotent migrations for ``instance/config.yaml``.

Unlike ``projects_migration`` (which generates a brand-new file), these
migrations rewrite an operator's existing, hand-edited config in place. They
are therefore **comment-preserving**: they patch the specific lines that
changed shape rather than round-tripping the document through PyYAML, which
would silently delete every comment in the file.

Each migration is a no-op when the config already has the current shape, so it
is safe to run on every startup.
"""

import re
import shutil
from pathlib import Path
from typing import List, Optional

import yaml

# Matches a top-level ``mcp:`` key (column 0), with an optional trailing
# comment. An inline flow list (``mcp: ["/a.json"]``) is captured separately so
# it can be migrated without walking following lines.
_MCP_BLOCK_RE = re.compile(r"^mcp:\s*(#.*)?$")
_MCP_INLINE_RE = re.compile(r"^mcp:[ \t]*(\[.*\])[ \t]*(#.*)?$")

# A block-sequence entry belonging to the ``mcp:`` block: ``- path`` at any
# indent. Blank lines and comments inside the block are carried along.
_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]")

# Indent given to migrated sequence entries under ``configs:``.
_INDENT_WIDTH = 4


def _config_path(koan_root: str) -> Path:
    return Path(koan_root) / "instance" / "config.yaml"


def _mcp_is_legacy_list(config_path: Path) -> tuple[bool, Optional[str]]:
    """Whether ``mcp`` parses as a list, plus why the check could not run.

    "No legacy list" and "the config could not be inspected" are different
    outcomes: the second leaves an operator believing a migration happened when
    the file was never read, so it is reported rather than silently skipped.
    """
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except OSError as e:
        return False, f"mcp: cannot read config.yaml ({e}); left unchanged"
    except yaml.YAMLError as e:
        return False, f"mcp: config.yaml is not valid YAML ({e}); left unchanged"
    return isinstance(data, dict) and isinstance(data.get("mcp"), list), None


def _rewrite_mcp_block(text: str) -> Optional[str]:
    """Return ``text`` with a legacy ``mcp:`` list nested under ``configs:``.

    Returns None when no top-level ``mcp:`` list is found, so the caller can
    skip the write entirely.
    """
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    changed = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n").rstrip("\r")

        inline = _MCP_INLINE_RE.match(stripped)
        if inline and not changed:
            trailing = f"  {inline.group(2)}" if inline.group(2) else ""
            out.append("mcp:\n")
            out.append(f"  configs: {inline.group(1)}{trailing}\n")
            changed = True
            i += 1
            continue

        if _MCP_BLOCK_RE.match(stripped) and not changed:
            # Look ahead: only a block sequence needs migrating. A mapping
            # (``  enabled: true``) is already in the current shape.
            body: List[str] = []
            j = i + 1
            saw_item = False
            while j < len(lines):
                nxt = lines[j].rstrip("\n").rstrip("\r")
                if not nxt.strip():
                    body.append(lines[j])
                    j += 1
                    continue
                if _LIST_ITEM_RE.match(nxt):
                    saw_item = True
                    body.append(lines[j])
                    j += 1
                    continue
                if nxt.lstrip().startswith("#") and nxt.startswith((" ", "\t")):
                    body.append(lines[j])
                    j += 1
                    continue
                break

            if not saw_item:
                out.append(line)
                i += 1
                continue

            # Trailing blank lines belong after the block, not inside it.
            while body and not body[-1].strip():
                body.pop()
                j -= 1

            # Re-indent the whole body so the first entry sits at 4 spaces
            # under ``configs:``, preserving any relative nesting below it.
            first = next(e for e in body if _LIST_ITEM_RE.match(e.rstrip("\r\n")))
            shift = " " * max(0, _INDENT_WIDTH - (len(first) - len(first.lstrip(" \t"))))

            out.append("mcp:\n")
            out.append("  configs:\n")
            out.extend(entry if not entry.strip() else shift + entry for entry in body)
            changed = True
            i = j
            continue

        out.append(line)
        i += 1

    return "".join(out) if changed else None


def migrate_mcp_config(koan_root: str) -> List[str]:
    """Convert a legacy ``mcp: [paths]`` list into ``mcp: {configs: [paths]}``.

    The MCP server work added ``mcp.enabled`` / ``mcp.tools_allow_destructive``
    beside the client config paths, which turned ``mcp`` into a mapping. Configs
    written before that change still hold a bare list. Runtime accepts both, but
    migrating keeps the operator's file matching the documented shape so future
    keys can be added by hand.

    A ``.bak`` copy is written next to the config before the first rewrite.

    Returns log messages describing what happened (empty when nothing to do).
    """
    config_path = _config_path(koan_root)
    if not config_path.exists():
        return []

    is_legacy, problem = _mcp_is_legacy_list(config_path)
    if problem:
        return [problem]
    if not is_legacy:
        return []

    try:
        original = config_path.read_text()
    except OSError as e:
        return [f"mcp: cannot read config.yaml ({e}); left unchanged"]

    migrated = _rewrite_mcp_block(original)
    if migrated is None:
        return [
            "mcp: legacy list detected but the 'mcp:' block could not be "
            "located; left unchanged (runtime still accepts the list form)"
        ]

    # Re-parse before writing: a rewrite that changes the parsed value is a bug,
    # and must never reach the operator's file.
    try:
        before = yaml.safe_load(original) or {}
        after = yaml.safe_load(migrated) or {}
    except yaml.YAMLError as e:
        return [f"mcp: migration produced invalid YAML ({e}); left unchanged"]

    expected = dict(before)
    expected["mcp"] = {"configs": before.get("mcp", [])}
    if after != expected:
        return ["mcp: migration changed more than the 'mcp' key; left unchanged"]

    from app.utils import atomic_write

    backup = config_path.with_suffix(".yaml.bak-mcp-mapping")
    try:
        if not backup.exists():
            shutil.copy2(config_path, backup)
        atomic_write(config_path, migrated)
    except OSError as e:
        return [f"mcp: cannot write config.yaml ({e}); left unchanged"]

    return [
        f"mcp: converted legacy list to 'mcp.configs' mapping "
        f"(backup: {backup.name})"
    ]


def run_config_migrations(koan_root: str) -> List[str]:
    """Run every config.yaml shape migration. Idempotent."""
    return migrate_mcp_config(koan_root)
