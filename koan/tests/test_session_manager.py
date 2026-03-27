"""Tests for session_manager.py — parallel session registry and lifecycle.

Mocks subprocess spawning (no real Claude calls per CLAUDE.md conventions).
"""

import json
import os
import subprocess
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from app.session_manager import (
    Session,
    SessionRegistry,
    SessionResult,
    get_max_parallel_sessions,
    kill_session,
    poll_sessions,
    recover_stale_sessions,
    spawn_session,
    _dict_to_session,
    SESSIONS_FILE,
)


@pytest.fixture
def instance_dir(tmp_path):
    """Create a minimal instance directory."""
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "missions.md").write_text("# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n")
    return str(inst)


@pytest.fixture
def registry(instance_dir):
    """Create a SessionRegistry."""
    return SessionRegistry(instance_dir)


@pytest.fixture
def sample_session():
    """Create a sample session for testing."""
    return Session(
        id="abc123",
        mission_text="Fix the auth bug",
        project_name="myproject",
        project_path="/tmp/project",
        worktree_path="/tmp/project/.worktrees/abc123",
        branch_name="koan/session-abc123",
        pid=12345,
        status="running",
        started_at=time.time(),
        stdout_file="/tmp/stdout.txt",
        stderr_file="/tmp/stderr.txt",
    )


class TestSessionRegistry:
    def test_register_and_get(self, registry, sample_session):
        registry.register(sample_session)
        retrieved = registry.get(sample_session.id)
        assert retrieved is not None
        assert retrieved.id == sample_session.id
        assert retrieved.mission_text == sample_session.mission_text

    def test_get_nonexistent(self, registry):
        assert registry.get("nonexistent") is None

    def test_update(self, registry, sample_session):
        registry.register(sample_session)
        sample_session.status = "done"
        sample_session.exit_code = 0
        registry.update(sample_session)
        retrieved = registry.get(sample_session.id)
        assert retrieved.status == "done"
        assert retrieved.exit_code == 0

    def test_remove(self, registry, sample_session):
        registry.register(sample_session)
        registry.remove(sample_session.id)
        assert registry.get(sample_session.id) is None

    def test_get_all(self, registry):
        s1 = Session(id="s1", mission_text="m1", project_name="p", project_path="/p",
                     worktree_path="/w1", branch_name="b1", status="running")
        s2 = Session(id="s2", mission_text="m2", project_name="p", project_path="/p",
                     worktree_path="/w2", branch_name="b2", status="done")
        registry.register(s1)
        registry.register(s2)
        all_sessions = registry.get_all()
        assert len(all_sessions) == 2
        ids = {s.id for s in all_sessions}
        assert ids == {"s1", "s2"}

    def test_get_active(self, registry):
        s1 = Session(id="s1", mission_text="m1", project_name="p", project_path="/p",
                     worktree_path="/w1", branch_name="b1", status="running")
        s2 = Session(id="s2", mission_text="m2", project_name="p", project_path="/p",
                     worktree_path="/w2", branch_name="b2", status="done")
        registry.register(s1)
        registry.register(s2)
        active = registry.get_active()
        assert len(active) == 1
        assert active[0].id == "s1"

    def test_get_by_project(self, registry):
        s1 = Session(id="s1", mission_text="m1", project_name="proj-a", project_path="/a",
                     worktree_path="/w1", branch_name="b1", status="running")
        s2 = Session(id="s2", mission_text="m2", project_name="proj-b", project_path="/b",
                     worktree_path="/w2", branch_name="b2", status="running")
        registry.register(s1)
        registry.register(s2)
        proj_a = registry.get_by_project("proj-a")
        assert len(proj_a) == 1
        assert proj_a[0].id == "s1"

    def test_clear_completed(self, registry):
        s1 = Session(id="s1", mission_text="m1", project_name="p", project_path="/p",
                     worktree_path="/w1", branch_name="b1", status="running")
        s2 = Session(id="s2", mission_text="m2", project_name="p", project_path="/p",
                     worktree_path="/w2", branch_name="b2", status="done")
        s3 = Session(id="s3", mission_text="m3", project_name="p", project_path="/p",
                     worktree_path="/w3", branch_name="b3", status="failed")
        registry.register(s1)
        registry.register(s2)
        registry.register(s3)
        registry.clear_completed()
        all_sessions = registry.get_all()
        assert len(all_sessions) == 1
        assert all_sessions[0].id == "s1"

    def test_persistence(self, instance_dir):
        """Data persists across registry instances."""
        reg1 = SessionRegistry(instance_dir)
        s = Session(id="persist", mission_text="m", project_name="p", project_path="/p",
                    worktree_path="/w", branch_name="b", status="running")
        reg1.register(s)

        reg2 = SessionRegistry(instance_dir)
        retrieved = reg2.get("persist")
        assert retrieved is not None
        assert retrieved.id == "persist"

    def test_handles_corrupt_file(self, instance_dir):
        """Gracefully handles corrupt sessions.json."""
        path = Path(instance_dir) / SESSIONS_FILE
        path.write_text("not valid json{{{")
        reg = SessionRegistry(instance_dir)
        assert reg.get_all() == []

    def test_handles_missing_file(self, instance_dir):
        reg = SessionRegistry(instance_dir)
        assert reg.get_all() == []


class TestDictToSession:
    def test_basic_conversion(self):
        d = {"id": "x", "mission_text": "m", "project_name": "p",
             "project_path": "/p", "worktree_path": "/w", "branch_name": "b"}
        s = _dict_to_session(d)
        assert s.id == "x"
        assert s.status == "pending"  # default

    def test_ignores_unknown_keys(self):
        d = {"id": "x", "mission_text": "m", "project_name": "p",
             "project_path": "/p", "worktree_path": "/w", "branch_name": "b",
             "unknown_field": "ignored"}
        s = _dict_to_session(d)
        assert s.id == "x"


class TestGetMaxParallelSessions:
    @patch("app.utils.load_config")
    def test_default(self, mock_config):
        mock_config.return_value = {}
        assert get_max_parallel_sessions() == 2

    @patch("app.utils.load_config")
    def test_configured(self, mock_config):
        mock_config.return_value = {"max_parallel_sessions": 3}
        assert get_max_parallel_sessions() == 3

    @patch("app.utils.load_config")
    def test_capped_at_max(self, mock_config):
        mock_config.return_value = {"max_parallel_sessions": 10}
        assert get_max_parallel_sessions() == 5

    @patch("app.utils.load_config")
    def test_minimum_one(self, mock_config):
        mock_config.return_value = {"max_parallel_sessions": 0}
        assert get_max_parallel_sessions() == 1


class TestPollSessions:
    def test_detects_completed(self, registry, sample_session):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        sample_session._proc = mock_proc
        sample_session._cleanup = MagicMock()

        # Create temp output files
        import tempfile
        fd, stdout = tempfile.mkstemp()
        os.write(fd, b"session output")
        os.close(fd)
        fd2, stderr = tempfile.mkstemp()
        os.write(fd2, b"")
        os.close(fd2)
        sample_session.stdout_file = stdout
        sample_session.stderr_file = stderr

        registry.register(sample_session)
        results = poll_sessions([sample_session], registry)

        assert len(results) == 1
        assert results[0].exit_code == 0
        assert results[0].session.status == "done"
        assert "session output" in results[0].stdout

        # Cleanup
        Path(stdout).unlink(missing_ok=True)
        Path(stderr).unlink(missing_ok=True)

    def test_still_running(self, registry, sample_session):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        sample_session._proc = mock_proc

        registry.register(sample_session)
        results = poll_sessions([sample_session], registry)
        assert len(results) == 0

    def test_failed_session(self, registry, sample_session):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        sample_session._proc = mock_proc
        sample_session._cleanup = MagicMock()
        sample_session.stdout_file = ""
        sample_session.stderr_file = ""

        registry.register(sample_session)
        results = poll_sessions([sample_session], registry)

        assert len(results) == 1
        assert results[0].exit_code == 1
        assert results[0].session.status == "failed"

    def test_no_proc_skipped(self, registry, sample_session):
        """Sessions without _proc attribute are skipped."""
        registry.register(sample_session)
        results = poll_sessions([sample_session], registry)
        assert len(results) == 0


class TestKillSession:
    def test_kills_process_and_updates_registry(self, registry, sample_session):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_proc.wait.return_value = None
        sample_session._proc = mock_proc
        sample_session._cleanup = MagicMock()

        registry.register(sample_session)

        with patch("os.getpgid", return_value=99999), \
             patch("os.killpg"), \
             patch("app.session_manager.remove_worktree"):
            kill_session(sample_session, registry)

        assert sample_session.status == "failed"
        retrieved = registry.get(sample_session.id)
        assert retrieved.status == "failed"

    def test_handles_already_dead_process(self, registry, sample_session):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # Already finished
        sample_session._proc = mock_proc
        sample_session._cleanup = MagicMock()

        registry.register(sample_session)

        with patch("app.session_manager.remove_worktree"):
            kill_session(sample_session, registry)

        assert sample_session.status == "failed"


class TestSpawnSessionFileHandleLeak:
    """Verify file handles are closed when spawn_session fails."""

    @patch("app.session_manager.inject_worktree_claude_md")
    @patch("app.session_manager.create_worktree")
    def test_out_f_closed_when_popen_raises(self, mock_create_wt, mock_inject, registry, tmp_path):
        """out_f and err_f are both closed when popen_cli() raises."""
        wt = MagicMock()
        wt.session_id = "test-leak"
        wt.path = str(tmp_path / "worktree")
        wt.branch = "koan/session-test-leak"
        mock_create_wt.return_value = wt

        opened_files = []
        real_open = open

        def tracking_open(path, mode="r", **kwargs):
            f = real_open(path, mode, **kwargs)
            opened_files.append(f)
            return f

        with patch("app.mission_runner.build_mission_command", return_value=["echo"]), \
             patch("builtins.open", side_effect=tracking_open), \
             patch("app.cli_exec.popen_cli", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                spawn_session(
                    mission_text="test",
                    project_name="p",
                    project_path=str(tmp_path),
                    instance_dir=registry.instance_dir,
                    registry=registry,
                )

        # Both file handles that were opened should be closed
        assert len(opened_files) == 2
        assert all(f.closed for f in opened_files), "leaked file handle(s)"

    @patch("app.session_manager.inject_worktree_claude_md")
    @patch("app.session_manager.create_worktree")
    def test_out_f_closed_when_stderr_open_raises(self, mock_create_wt, mock_inject, registry, tmp_path):
        """out_f is closed when the second open() (stderr) raises."""
        wt = MagicMock()
        wt.session_id = "test-leak2"
        wt.path = str(tmp_path / "worktree")
        wt.branch = "koan/session-test-leak2"
        mock_create_wt.return_value = wt

        opened_files = []
        real_open = open
        call_count = 0

        def open_fail_second(path, mode="r", **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("disk full")
            f = real_open(path, mode, **kwargs)
            opened_files.append(f)
            return f

        with patch("app.mission_runner.build_mission_command", return_value=["echo"]), \
             patch("builtins.open", side_effect=open_fail_second):
            with pytest.raises(OSError, match="disk full"):
                spawn_session(
                    mission_text="test",
                    project_name="p",
                    project_path=str(tmp_path),
                    instance_dir=registry.instance_dir,
                    registry=registry,
                )

        # The first file handle (out_f) must be closed despite second open failing
        assert len(opened_files) == 1
        assert opened_files[0].closed, "out_f leaked when stderr open() raised"


class TestRecoverStaleSessions:
    def test_marks_dead_sessions_as_failed(self, registry):
        s = Session(id="dead", mission_text="m", project_name="p",
                    project_path="/tmp/fake", worktree_path="/tmp/fake/wt",
                    branch_name="b", status="running", pid=999999)
        registry.register(s)

        with patch("os.kill", side_effect=ProcessLookupError), \
             patch("app.session_manager.remove_worktree"):
            recover_stale_sessions(registry)

        retrieved = registry.get("dead")
        assert retrieved.status == "failed"

    def test_leaves_alive_sessions(self, registry):
        s = Session(id="alive", mission_text="m", project_name="p",
                    project_path="/tmp/fake", worktree_path="/tmp/fake/wt",
                    branch_name="b", status="running", pid=os.getpid())
        registry.register(s)

        recover_stale_sessions(registry)

        retrieved = registry.get("alive")
        assert retrieved.status == "running"

    def test_handles_zero_pid(self, registry):
        s = Session(id="nopid", mission_text="m", project_name="p",
                    project_path="/tmp/fake", worktree_path="/tmp/fake/wt",
                    branch_name="b", status="running", pid=0)
        registry.register(s)

        recover_stale_sessions(registry)

        retrieved = registry.get("nopid")
        assert retrieved.status == "failed"
