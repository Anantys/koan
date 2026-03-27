"""Launchd plist template rendering for macOS auto-start.

Provides testable logic for generating launchd plist files with
placeholder substitution, mirroring the systemd_service.py approach.
"""

import glob
import os
import sys


def _validate_placeholder(name: str, value: str) -> str:
    """Validate a placeholder value for safe plist template substitution.

    Rejects values containing characters that could inject plist elements.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"Placeholder {name} must not contain newlines")
    return value


def render_plist_template(template_path: str, koan_root: str) -> str:
    """Render a single .plist.template file with placeholder substitution."""
    _validate_placeholder("__KOAN_ROOT__", koan_root)

    with open(template_path, "r") as f:
        content = f.read()
    content = content.replace("__KOAN_ROOT__", koan_root)
    return content


def render_all_templates(template_dir: str, koan_root: str) -> dict:
    """Render all com.koan.*.plist.template files in a directory.

    Returns dict mapping plist filename (without .template) to content.
    """
    pattern = os.path.join(template_dir, "com.koan.*.plist.template")
    results = {}
    for template_path in sorted(glob.glob(pattern)):
        plist_name = os.path.basename(template_path).replace(".template", "")
        results[plist_name] = render_plist_template(template_path, koan_root)
    return results


def get_launchd_dir() -> str:
    """Return the user-level LaunchAgents directory."""
    return os.path.expanduser("~/Library/LaunchAgents")


def main():
    """CLI entrypoint: render plist templates to an output directory.

    Usage: python -m app.launchd_service <koan_root> <output_dir>
    """
    if len(sys.argv) != 3:
        print(
            "Usage: python -m app.launchd_service <koan_root> <output_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    koan_root, output_dir = sys.argv[1:3]

    template_dir = os.path.join(os.path.dirname(__file__), "..", "launchd")
    rendered = render_all_templates(template_dir, koan_root)

    os.makedirs(output_dir, exist_ok=True)
    for plist_name, content in rendered.items():
        out_path = os.path.join(output_dir, plist_name)
        with open(out_path, "w") as f:
            f.write(content)
        os.chmod(out_path, 0o644)
        print(f"→ Generated {plist_name}")


if __name__ == "__main__":
    main()
