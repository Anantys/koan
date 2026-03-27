"""Tests for app.rename_project — project renaming tool."""

import json
import os
import textwrap

import pytest

from app.rename_project import (
    find_instance_files,
    rename_journal_files,
    rename_memory_dir,
    rename_project_key_in_yaml,
    replace_in_file,
    run_rename,
)


@pytest.fixture
def koan_root(tmp_path):
    """Create a minimal KOAN_ROOT with projects.yaml and instance/."""
    # projects.yaml
    (tmp_path / "projects.yaml").write_text(textwrap.dedent("""\
        defaults:
          git_auto_merge:
            enabled: false
        projects:
          anantys-back:
            path: /some/path/anantys-back
            github_url: anantys/investmindr
          anantys-front:
            path: /some/path/anantys-front
          koan:
            path: /some/path/koan
    """))

    # instance structure
    inst = tmp_path / "instance"
    inst.mkdir()

    # missions.md
    (inst / "missions.md").write_text(textwrap.dedent("""\
        # Pending
        - [project:anantys-back] fix the bug

        # In Progress

        # Done
        - [project:anantys-back] deploy feature ✅ (2026-03-25)
        - [project:anantys-front] update UI ✅ (2026-03-25)
    """))

    # memory dirs
    mem = inst / "memory" / "projects"
    (mem / "anantys-back").mkdir(parents=True)
    (mem / "anantys-back" / "priorities.md").write_text("priorities for anantys-back")
    (mem / "anantys-front").mkdir(parents=True)
    (mem / "anantys-front" / "learnings.md").write_text("learnings for anantys-front")
    (mem / "koan").mkdir(parents=True)

    # journal files
    journal = inst / "journal" / "2026-03-25"
    journal.mkdir(parents=True)
    (journal / "anantys-back.md").write_text("## anantys-back journal")
    (journal / "koan.md").write_text("## koan journal")

    archives = inst / "journal" / "archives" / "2026-02"
    archives.mkdir(parents=True)
    (archives / "anantys-back.md").write_text("archived anantys-back")

    # JSON files
    (inst / "mission_history.json").write_text(json.dumps([
        {"project": "anantys-back", "mission": "fix bug"},
        {"project": "koan", "mission": "update"},
    ]))

    (inst / "recurring.json").write_text(json.dumps([
        {"text": "[project:anantys-back] weekly check", "interval": "7d"},
    ]))

    return tmp_path


class TestRenameProjectKeyInYaml:
    def test_renames_key(self, koan_root):
        yaml_path = koan_root / "projects.yaml"
        result = rename_project_key_in_yaml(yaml_path, "anantys-back", "aback")
        assert "  aback:" in result
        assert "  anantys-back:" not in result
        # Other projects untouched
        assert "  anantys-front:" in result
        assert "  koan:" in result

    def test_missing_project_raises(self, koan_root):
        yaml_path = koan_root / "projects.yaml"
        with pytest.raises(ValueError, match="not found"):
            rename_project_key_in_yaml(yaml_path, "nonexistent", "new")

    def test_preserves_content(self, koan_root):
        yaml_path = koan_root / "projects.yaml"
        result = rename_project_key_in_yaml(yaml_path, "anantys-back", "aback")
        assert "anantys/investmindr" in result
        assert "/some/path/anantys-back" in result


class TestReplaceInFile:
    def test_replaces_project_tags(self, tmp_path):
        f = tmp_path / "missions.md"
        f.write_text("[project:old-name] do stuff\n[project:other] keep\n")
        changes = replace_in_file(f, "old-name", "new-name")
        assert len(changes) == 1
        assert changes[0][0] == 1  # line number
        assert "[project:new-name]" in changes[0][2]

    def test_replaces_json_project(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"project": "myproj", "x": 1}\n')
        changes = replace_in_file(f, "myproj", "newproj")
        assert len(changes) == 1
        assert '"project": "newproj"' in changes[0][2]

    def test_no_false_positives(self, tmp_path):
        f = tmp_path / "other.md"
        f.write_text("anantys-back is mentioned but not as a project tag\n")
        changes = replace_in_file(f, "anantys-back", "aback")
        assert len(changes) == 0


class TestRenameMemoryDir:
    def test_renames_dir(self, koan_root):
        inst = koan_root / "instance"
        rename_memory_dir(inst, "anantys-back", "aback", dry_run=False)
        assert (inst / "memory" / "projects" / "aback").is_dir()
        assert not (inst / "memory" / "projects" / "anantys-back").exists()
        assert (inst / "memory" / "projects" / "aback" / "priorities.md").exists()

    def test_dry_run_no_change(self, koan_root):
        inst = koan_root / "instance"
        rename_memory_dir(inst, "anantys-back", "aback", dry_run=True)
        assert (inst / "memory" / "projects" / "anantys-back").is_dir()
        assert not (inst / "memory" / "projects" / "aback").exists()

    def test_missing_dir_returns_false(self, koan_root):
        inst = koan_root / "instance"
        result = rename_memory_dir(inst, "nonexistent", "new", dry_run=False)
        assert result is False


class TestRenameJournalFiles:
    def test_renames_journal_files(self, koan_root):
        inst = koan_root / "instance"
        renamed = rename_journal_files(inst, "anantys-back", "aback", dry_run=False)
        assert len(renamed) == 2  # main + archives
        assert (inst / "journal" / "2026-03-25" / "aback.md").exists()
        assert not (inst / "journal" / "2026-03-25" / "anantys-back.md").exists()

    def test_dry_run_no_change(self, koan_root):
        inst = koan_root / "instance"
        renamed = rename_journal_files(inst, "anantys-back", "aback", dry_run=True)
        assert len(renamed) == 2
        assert (inst / "journal" / "2026-03-25" / "anantys-back.md").exists()


class TestRunRename:
    def test_dry_run_changes_nothing(self, koan_root):
        run_rename(koan_root, "anantys-back", "aback", dry_run=True)
        # Nothing should have changed
        yaml_text = (koan_root / "projects.yaml").read_text()
        assert "  anantys-back:" in yaml_text
        missions = (koan_root / "instance" / "missions.md").read_text()
        assert "[project:anantys-back]" in missions

    def test_apply_renames_everything(self, koan_root):
        run_rename(koan_root, "anantys-back", "aback", dry_run=False)

        # projects.yaml updated
        yaml_text = (koan_root / "projects.yaml").read_text()
        assert "  aback:" in yaml_text
        assert "  anantys-back:" not in yaml_text

        # missions.md updated
        missions = (koan_root / "instance" / "missions.md").read_text()
        assert "[project:aback]" in missions
        assert "[project:anantys-back]" not in missions

        # Memory dir renamed
        assert (koan_root / "instance" / "memory" / "projects" / "aback").is_dir()
        assert not (koan_root / "instance" / "memory" / "projects" / "anantys-back").exists()

        # Journal files renamed
        assert (koan_root / "instance" / "journal" / "2026-03-25" / "aback.md").exists()

        # JSON content updated
        history = json.loads((koan_root / "instance" / "mission_history.json").read_text())
        assert history[0]["project"] == "aback"
        assert history[1]["project"] == "koan"  # untouched

        # recurring.json updated
        recurring = json.loads((koan_root / "instance" / "recurring.json").read_text())
        assert "[project:aback]" in recurring[0]["text"]

        # Other projects untouched
        assert "  anantys-front:" in yaml_text
        assert "  koan:" in yaml_text

    def test_nonexistent_project_exits(self, koan_root):
        with pytest.raises(SystemExit):
            run_rename(koan_root, "nonexistent", "aback", dry_run=True)

    def test_duplicate_target_exits(self, koan_root):
        with pytest.raises(SystemExit):
            run_rename(koan_root, "anantys-back", "koan", dry_run=True)
