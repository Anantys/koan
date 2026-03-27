"""Systemd service PATH building and template rendering.

Provides testable logic for generating systemd service files with a
properly sanitized PATH that preserves the caller's environment.
"""

import glob
import os
import sys

ALLOWED_HOME_SUBDIRS = [
    ".local/bin",  # XDG standard; houses pip-installed CLIs like claude
]

ESSENTIAL_DIRS = [
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
]


def build_safe_path(raw_path: str, home_dir: str) -> str:
    """Build a sanitized PATH for systemd services.

    Filters out home-directory entries for security, ensures essential
    system directories are present, and deduplicates while preserving order.
    """
    home = home_dir.rstrip("/")
    seen = set()
    result = []

    for entry in raw_path.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        if entry == home or entry.startswith(home + "/"):
            rel = entry[len(home) + 1:]
            if rel not in ALLOWED_HOME_SUBDIRS:
                continue
        if entry not in seen:
            seen.add(entry)
            result.append(entry)

    for d in ESSENTIAL_DIRS:
        if d not in seen:
            seen.add(d)
            result.append(d)

    return ":".join(result)


def _validate_placeholder(name: str, value: str) -> str:
    """Validate a placeholder value for safe systemd template substitution.

    Rejects values containing characters that could inject systemd directives.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"Placeholder {name} must not contain newlines")
    return value


def render_service_template(
    template_path: str,
    koan_root: str,
    python_path: str,
    safe_path: str,
    user: str = "",
    group: str = "",
) -> str:
    """Render a single .service.template file with placeholder substitution."""
    _validate_placeholder("__KOAN_ROOT__", koan_root)
    _validate_placeholder("__PYTHON__", python_path)
    _validate_placeholder("__PATH__", safe_path)

    with open(template_path, "r") as f:
        content = f.read()
    content = content.replace("__KOAN_ROOT__", koan_root)
    content = content.replace("__PYTHON__", python_path)
    content = content.replace("__PATH__", safe_path)
    content = content.replace("__USER__", user or "root")
    content = content.replace("__GROUP__", group or "root")
    return content


def render_all_templates(
    template_dir: str,
    koan_root: str,
    python_path: str,
    safe_path: str,
    user: str = "",
    group: str = "",
) -> dict:
    """Render all koan*.service.template files in a directory.

    Returns dict mapping service filename (without .template) to content.
    """
    pattern = os.path.join(template_dir, "koan*.service.template")
    results = {}
    for template_path in sorted(glob.glob(pattern)):
        service_name = os.path.basename(template_path).replace(".template", "")
        results[service_name] = render_service_template(
            template_path, koan_root, python_path, safe_path,
            user=user, group=group,
        )
    return results


def main():
    """CLI entrypoint: render templates to an output directory.

    Usage: python -m app.systemd_service <koan_root> <python_path> <caller_path> <output_dir>
    """
    if len(sys.argv) != 5:
        print(
            "Usage: python -m app.systemd_service <koan_root> <python_path> <caller_path> <output_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    koan_root, python_path, caller_path, output_dir = sys.argv[1:5]

    home_dir = os.path.expanduser("~")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        home_dir = os.path.expanduser(f"~{sudo_user}")

    safe_path = build_safe_path(caller_path, home_dir)

    # Resolve runtime user: SUDO_USER (the human who ran sudo) or current user
    run_user = sudo_user or os.environ.get("USER", "root")
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(run_user)
        run_group = grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        run_group = run_user

    template_dir = os.path.join(os.path.dirname(__file__), "..", "systemd")
    rendered = render_all_templates(
        template_dir, koan_root, python_path, safe_path,
        user=run_user, group=run_group,
    )

    os.makedirs(output_dir, exist_ok=True)
    for service_name, content in rendered.items():
        out_path = os.path.join(output_dir, service_name)
        with open(out_path, "w") as f:
            f.write(content)
        os.chmod(out_path, 0o644)
        print(f"→ Generated {service_name} (User={run_user}, Group={run_group})")


if __name__ == "__main__":
    main()
