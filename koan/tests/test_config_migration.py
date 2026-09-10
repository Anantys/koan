"""Tests for the one-shot config.yaml shape migrations."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_migration import run_config_migrations  # noqa: E402
from app.config_validator import validate_config_or_raise  # noqa: E402


@pytest.fixture
def koan_root(tmp_path):
    (tmp_path / "instance").mkdir()
    return tmp_path


def write_config(koan_root, text):
    path = koan_root / "instance" / "config.yaml"
    path.write_text(text)
    return path


def test_no_config_file_is_a_noop(koan_root):
    assert run_config_migrations(str(koan_root)) == []


def test_missing_mcp_key_is_a_noop(koan_root):
    path = write_config(koan_root, "interval_seconds: 60\n")
    assert run_config_migrations(str(koan_root)) == []
    assert path.read_text() == "interval_seconds: 60\n"


def test_mapping_form_is_a_noop(koan_root):
    original = "mcp:\n  enabled: true\n  configs:\n    - /a.json\n"
    path = write_config(koan_root, original)
    assert run_config_migrations(str(koan_root)) == []
    assert path.read_text() == original


def test_legacy_list_is_nested_under_configs(koan_root):
    path = write_config(
        koan_root,
        "max_runs_per_day: 80\n"
        "mcp:\n"
        "- /root/instance/.mcp-jira.json\n"
        "messaging:\n"
        "  provider: slack\n",
    )

    msgs = run_config_migrations(str(koan_root))

    assert len(msgs) == 1
    assert "mcp.configs" in msgs[0]
    assert path.read_text() == (
        "max_runs_per_day: 80\n"
        "mcp:\n"
        "  configs:\n"
        "    - /root/instance/.mcp-jira.json\n"
        "messaging:\n"
        "  provider: slack\n"
    )


def test_indented_list_items_are_migrated(koan_root):
    path = write_config(koan_root, "mcp:\n  - /a.json\n  - /b.json\n")

    run_config_migrations(str(koan_root))

    assert yaml.safe_load(path.read_text()) == {
        "mcp": {"configs": ["/a.json", "/b.json"]}
    }


def test_inline_flow_list_is_migrated(koan_root):
    path = write_config(koan_root, 'mcp: ["/a.json"]  # jira\n')

    run_config_migrations(str(koan_root))

    assert '# jira' in path.read_text()
    assert yaml.safe_load(path.read_text()) == {"mcp": {"configs": ["/a.json"]}}


def test_comments_are_preserved(koan_root):
    path = write_config(
        koan_root,
        "# top comment\n"
        "interval_seconds: 60  # inline\n"
        "\n"
        "# mcp section\n"
        "mcp:\n"
        "- /a.json\n"
        "\n"
        "# after\n"
        "telegram:\n"
        "  enabled: true\n",
    )

    run_config_migrations(str(koan_root))
    text = path.read_text()

    for comment in ("# top comment", "# inline", "# mcp section", "# after"):
        assert comment in text
    assert yaml.safe_load(text)["telegram"] == {"enabled": True}


def test_a_backup_is_written_once(koan_root):
    write_config(koan_root, "mcp:\n- /a.json\n")
    backup = koan_root / "instance" / "config.yaml.bak-mcp-mapping"

    run_config_migrations(str(koan_root))

    assert backup.exists()
    assert yaml.safe_load(backup.read_text()) == {"mcp": ["/a.json"]}


def test_migration_is_idempotent(koan_root):
    path = write_config(koan_root, "mcp:\n- /a.json\n")

    assert run_config_migrations(str(koan_root)) != []
    after_first = path.read_text()
    assert run_config_migrations(str(koan_root)) == []
    assert path.read_text() == after_first


def test_only_the_top_level_mcp_key_is_touched(koan_root):
    """A nested ``mcp:`` list under projects: must not be rewritten."""
    path = write_config(
        koan_root,
        "mcp:\n"
        "- /global.json\n"
        "projects:\n"
        "  demo:\n"
        "    mcp:\n"
        "    - /per-project.json\n",
    )

    run_config_migrations(str(koan_root))
    data = yaml.safe_load(path.read_text())

    assert data["mcp"] == {"configs": ["/global.json"]}
    assert data["projects"]["demo"]["mcp"] == ["/per-project.json"]


def test_unparseable_config_is_left_alone(koan_root):
    original = "mcp:\n- /a.json\n  bad: [\n"
    path = write_config(koan_root, original)

    assert run_config_migrations(str(koan_root)) == []
    assert path.read_text() == original


def test_migrated_config_passes_strict_validation(koan_root):
    """The end-to-end contract: a legacy config starts the agent after migrating."""
    write_config(koan_root, "mcp:\n- /a.json\n")

    run_config_migrations(str(koan_root))

    validate_config_or_raise(str(koan_root))  # must not raise
