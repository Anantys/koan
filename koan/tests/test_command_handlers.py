"""Tests for app.command_handlers — Telegram bridge command handlers."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def koan_root(tmp_path):
    """Create a minimal koan root with instance directory."""
    instance = tmp_path / "instance"
    instance.mkdir()
    missions = instance / "missions.md"
    missions.write_text(
        "# Missions\n\n## Pending\n\n## In Progress\n\n## Done\n"
    )
    return tmp_path


@pytest.fixture
def patch_bridge_state(koan_root):
    """Patch bridge_state module-level variables for command_handlers."""
    instance = koan_root / "instance"
    missions_file = instance / "missions.md"

    with patch("app.command_handlers.KOAN_ROOT", koan_root), \
         patch("app.command_handlers.INSTANCE_DIR", instance), \
         patch("app.command_handlers.MISSIONS_FILE", missions_file):
        yield koan_root


@pytest.fixture
def mock_send():
    """Mock send_telegram."""
    with patch("app.command_handlers.send_telegram") as m:
        yield m


@pytest.fixture
def mock_registry():
    """Mock skill registry."""
    registry = MagicMock()
    registry.find_by_command.return_value = None
    registry.resolve_scoped_command.return_value = None
    registry.suggest_command.return_value = None
    registry.list_all.return_value = []
    registry.list_by_scope.return_value = []
    with patch("app.command_handlers._get_registry", return_value=registry):
        yield registry


# ---------------------------------------------------------------------------
# Test: handle_command routing
# ---------------------------------------------------------------------------

class TestHandleCommandRouting:
    """Tests for the main handle_command() dispatch."""

    def test_stop_command_creates_stop_file(self, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/stop")
        stop_file = patch_bridge_state / ".koan-stop"
        assert stop_file.exists()
        assert stop_file.read_text() == "STOP"
        mock_send.assert_called_once()
        assert "Stop requested" in mock_send.call_args[0][0]

    def test_cycle_command_creates_cycle_file(self, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/cycle")
        cycle_file = patch_bridge_state / ".koan-cycle"
        assert cycle_file.exists()
        assert cycle_file.read_text() == "CYCLE"
        mock_send.assert_called_once()
        assert "Cycle requested" in mock_send.call_args[0][0]

    def test_pause_command_creates_pause_file(self, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/pause")
        assert (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "Paused" in mock_send.call_args[0][0]

    def test_sleep_alias_creates_pause_file(self, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/sleep")
        assert (patch_bridge_state / ".koan-pause").exists()

    def test_pause_when_already_paused(self, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        (patch_bridge_state / ".koan-pause").touch()
        handle_command("/pause")
        mock_send.assert_called_once()
        assert "Already paused" in mock_send.call_args[0][0]

    @patch("app.command_handlers.handle_resume")
    def test_resume_command_calls_handle_resume(self, mock_resume, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/resume")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_work_alias(self, mock_resume, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/work")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_awake_alias(self, mock_resume, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/awake")
        mock_resume.assert_called_once()

    @patch("app.command_handlers.handle_resume")
    def test_run_alias(self, mock_resume, patch_bridge_state, mock_send):
        from app.command_handlers import handle_command
        handle_command("/run")
        mock_resume.assert_called_once()

    def test_unknown_command_sends_error(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        from app.command_handlers import handle_command
        handle_command("/nonexistent")
        mock_send.assert_called_once()
        assert "Unknown command" in mock_send.call_args[0][0]
        assert "/nonexistent" in mock_send.call_args[0][0]

    def test_unknown_command_suggests_closest_match(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        from app.command_handlers import handle_command
        mock_registry.suggest_command.return_value = "status"
        handle_command("/statu")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Did you mean /status?" in msg

    def test_unknown_command_no_suggestion_when_no_match(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        from app.command_handlers import handle_command
        mock_registry.suggest_command.return_value = None
        handle_command("/xyzzy")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Did you mean" not in msg

    def test_group_name_as_command_shows_group_help(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """/missions (group name) should expand the group, not error."""
        from app.command_handlers import handle_command
        from app.skills import Skill, SkillCommand
        skill = MagicMock(spec=Skill)
        skill.description = "Create or manage missions"
        cmd = MagicMock(spec=SkillCommand)
        cmd.name = "mission"
        cmd.description = "Create a mission"
        cmd.aliases = []
        skill.commands = [cmd]
        mock_registry.list_by_group.return_value = [skill]
        handle_command("/missions")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Missions" in msg
        assert "❌ Unknown command" not in msg

    def test_help_command(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import handle_command
        handle_command("/help")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Help" in msg
        assert "/help <group>" in msg

    def test_help_specific_command(self, patch_bridge_state, mock_send, mock_registry):
        """Test /help <command> shows detailed help for a skill."""
        from app.command_handlers import handle_command
        from app.skills import Skill, SkillCommand
        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(
            name="quota",
            description="Show quota usage",
            aliases=["qu"],
            usage="/quota [project]",
        )
        skill.commands = [cmd]
        skill.description = "Show quota usage"
        mock_registry.find_by_command.return_value = skill

        # "quota" is not a group name, so it falls through to L3 command help
        handle_command("/help quota")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "/quota" in msg
        assert "Show quota usage" in msg

    def test_help_group_name_shows_group(self, patch_bridge_state, mock_send, mock_registry):
        """When arg matches a group name, L2 group view takes priority."""
        from app.command_handlers import handle_command
        mock_registry.list_by_group.return_value = []

        handle_command("/help status")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Status" in msg

    def test_help_command_with_inconsistent_registry(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """If find_by_command returns a skill whose commands don't match,
        the handler should gracefully send an error instead of crashing."""
        from app.command_handlers import handle_command
        from app.skills import Skill, SkillCommand

        # Use "foobar" which is not a group name, so it falls through to L3
        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(name="different", description="Mismatch")
        skill.commands = [cmd]
        mock_registry.find_by_command.return_value = skill

        # Should not raise StopIteration
        handle_command("/help foobar")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg


# ---------------------------------------------------------------------------
# Test: project-name fallback for unknown commands
# ---------------------------------------------------------------------------

class TestProjectNameFallback:
    """Tests for the fallback that rewrites '/project context' as a mission."""

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"koan", "backend"})
    @patch("app.command_handlers.handle_mission")
    def test_project_name_with_args_becomes_mission(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """'/koan fix the bug' → handle_mission('koan fix the bug')."""
        from app.command_handlers import handle_command
        handle_command("/koan fix the bug")
        mock_mission.assert_called_once_with("koan fix the bug")
        mock_send.assert_not_called()

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"koan"})
    @patch("app.command_handlers.handle_mission")
    def test_project_name_without_args_stays_unknown(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """'/koan' alone (no context) should NOT fallback — stays unknown."""
        from app.command_handlers import handle_command
        handle_command("/koan")
        mock_mission.assert_not_called()
        mock_send.assert_called_once()
        assert "Unknown command" in mock_send.call_args[0][0]

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"koan"})
    @patch("app.command_handlers.handle_mission")
    def test_unknown_name_not_a_project_stays_unknown(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """'/foobar do stuff' where foobar is not a project → unknown command."""
        from app.command_handlers import handle_command
        handle_command("/foobar do stuff")
        mock_mission.assert_not_called()
        mock_send.assert_called_once()
        assert "Unknown command" in mock_send.call_args[0][0]

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"backend"})
    @patch("app.command_handlers.handle_mission")
    def test_project_name_case_insensitive(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """'/BACKEND fix it' should match even with different casing."""
        from app.command_handlers import handle_command
        handle_command("/BACKEND fix it")
        mock_mission.assert_called_once_with("backend fix it")

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"status"})
    def test_existing_skill_takes_priority_over_project(
        self, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """If 'status' is both a skill and a project, skill wins."""
        from app.command_handlers import handle_command
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = False
        mock_registry.find_by_command.return_value = skill

        with patch("app.command_handlers.execute_skill", return_value="ok") as mock_exec, \
             patch("app.command_handlers.handle_mission") as mock_mission:
            handle_command("/status check things")
            mock_exec.assert_called_once()
            mock_mission.assert_not_called()

    @patch("app.command_handlers.is_known_project",
           side_effect=lambda n: n.lower() in {"koan", "tmf"})
    @patch("app.command_handlers.handle_mission")
    def test_project_fallback_with_multiword_context(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """'/tmf fix the broken test in auth module' passes full context."""
        from app.command_handlers import handle_command
        handle_command("/tmf fix the broken test in auth module")
        mock_mission.assert_called_once_with("tmf fix the broken test in auth module")

    @patch("app.command_handlers.is_known_project", return_value=False)
    @patch("app.command_handlers.handle_mission")
    def test_no_projects_configured_still_unknown(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """When no projects are configured, fallback doesn't trigger."""
        from app.command_handlers import handle_command
        handle_command("/anything do stuff")
        mock_mission.assert_not_called()
        assert "Unknown command" in mock_send.call_args[0][0]

    @patch("app.command_handlers.get_known_projects",
           side_effect=Exception("config broken"))
    @patch("app.command_handlers.handle_mission")
    def test_project_lookup_failure_falls_through(
        self, mock_mission, mock_projects,
        patch_bridge_state, mock_send, mock_registry
    ):
        """If get_known_projects raises, gracefully fall through to unknown."""
        from app.command_handlers import handle_command
        handle_command("/koan fix stuff")
        mock_mission.assert_not_called()
        assert "Unknown command" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: is_known_project helper (now in utils.py)
# ---------------------------------------------------------------------------

class TestIsKnownProject:
    """Tests for the is_known_project helper function (shared utility)."""

    @patch("app.utils.get_known_projects",
           return_value=[("koan", "/path"), ("backend", "/path2")])
    def test_known_project_returns_true(self, mock_projects):
        from app.utils import is_known_project
        assert is_known_project("koan") is True
        assert is_known_project("backend") is True

    @patch("app.utils.get_known_projects",
           return_value=[("koan", "/path")])
    def test_unknown_name_returns_false(self, mock_projects):
        from app.utils import is_known_project
        assert is_known_project("foobar") is False

    @patch("app.utils.get_known_projects",
           return_value=[("Koan", "/path")])
    def test_case_insensitive(self, mock_projects):
        from app.utils import is_known_project
        assert is_known_project("koan") is True
        assert is_known_project("KOAN") is True

    @patch("app.utils.get_known_projects", return_value=[])
    def test_empty_projects(self, mock_projects):
        from app.utils import is_known_project
        assert is_known_project("anything") is False

    @patch("app.utils.get_known_projects",
           side_effect=Exception("boom"))
    def test_exception_returns_false(self, mock_projects):
        from app.utils import is_known_project
        assert is_known_project("koan") is False


# ---------------------------------------------------------------------------
# Test: handle_resume
# ---------------------------------------------------------------------------

@patch("app.command_handlers._is_runner_alive", return_value=True)
class TestHandleResume:
    """Tests for handle_resume — unpause from various states."""

    def test_resume_manual_pause(self, mock_alive, patch_bridge_state, mock_send):
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").touch()
        handle_resume()
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "Unpaused" in mock_send.call_args[0][0]

    def test_resume_max_runs_pause(self, mock_alive, patch_bridge_state, mock_send):
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("max_runs\n")
        handle_resume()
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "max_runs" in mock_send.call_args[0][0]

    @patch("app.command_handlers._reset_session_counters")
    def test_resume_quota_pause_resets_counters(
        self, mock_reset, mock_alive, patch_bridge_state, mock_send
    ):
        from app.command_handlers import handle_resume
        # Quota reason with a far future timestamp
        future_ts = int(time.time()) + 7200
        (patch_bridge_state / ".koan-pause").write_text(
            f"quota\n{future_ts}\nresets at 10am"
        )
        handle_resume()
        mock_reset.assert_called_once()
        assert not (patch_bridge_state / ".koan-pause").exists()

    @patch("app.command_handlers._reset_session_counters")
    def test_resume_quota_with_expired_reset(
        self, mock_reset, mock_alive, patch_bridge_state, mock_send
    ):
        from app.command_handlers import handle_resume
        # Quota reason with past timestamp (expired)
        past_ts = int(time.time()) - 3600
        (patch_bridge_state / ".koan-pause").write_text(
            f"quota\n{past_ts}\nalready reset"
        )
        handle_resume()
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Quota should be reset" in msg

    def test_resume_when_not_paused(self, mock_alive, patch_bridge_state, mock_send):
        from app.command_handlers import handle_resume
        handle_resume()
        mock_send.assert_called_once()
        assert "No pause" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: _handle_start
# ---------------------------------------------------------------------------

class TestHandleStart:
    """Tests for /start command."""

    @patch("app.command_handlers.handle_resume")
    @patch("app.pid_manager.check_pidfile", return_value=1234)
    def test_start_when_paused_calls_resume(
        self, mock_pid, mock_resume, patch_bridge_state, mock_send
    ):
        from app.command_handlers import _handle_start
        (patch_bridge_state / ".koan-pause").touch()
        _handle_start()
        mock_resume.assert_called_once()

    @patch("app.pid_manager.check_pidfile", return_value=5678)
    def test_start_when_running_says_already_running(
        self, mock_pid, patch_bridge_state, mock_send
    ):
        from app.command_handlers import _handle_start
        _handle_start()
        mock_send.assert_called_once()
        assert "already running" in mock_send.call_args[0][0]

    @patch("app.pid_manager.start_runner", return_value=(True, "Runner started (PID 9999)"))
    @patch("app.pid_manager.check_pidfile", return_value=None)
    def test_start_when_stopped_launches_runner(
        self, mock_pid, mock_start, patch_bridge_state, mock_send
    ):
        from app.command_handlers import _handle_start
        _handle_start()
        mock_start.assert_called_once()
        # Should send two messages: "Starting..." and success
        assert mock_send.call_count == 2

    @patch("app.pid_manager.start_runner", return_value=(False, "Failed to start"))
    @patch("app.pid_manager.check_pidfile", return_value=None)
    def test_start_failure(
        self, mock_pid, mock_start, patch_bridge_state, mock_send
    ):
        from app.command_handlers import _handle_start
        _handle_start()
        calls = [c[0][0] for c in mock_send.call_args_list]
        assert any("❌" in c for c in calls)


# ---------------------------------------------------------------------------
# Test: handle_mission
# ---------------------------------------------------------------------------

class TestHandleMission:
    """Tests for handle_mission — mission queuing from Telegram."""

    @patch("app.command_handlers.insert_pending_mission")
    def test_simple_mission(self, mock_insert, patch_bridge_state, mock_send):
        from app.command_handlers import handle_mission
        handle_mission("fix the login bug")
        mock_insert.assert_called_once()
        entry = mock_insert.call_args[0][1]
        assert "fix the login bug" in entry
        mock_send.assert_called_once()
        assert "Mission received" in mock_send.call_args[0][0]

    @patch("app.command_handlers.insert_pending_mission")
    def test_mission_with_project_tag(self, mock_insert, patch_bridge_state, mock_send):
        from app.command_handlers import handle_mission
        handle_mission("[project:koan] fix the login bug")
        entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in entry
        assert "fix the login bug" in entry

    @patch("app.command_handlers.insert_pending_mission")
    def test_mission_strips_mission_prefix(self, mock_insert, patch_bridge_state, mock_send):
        from app.command_handlers import handle_mission
        handle_mission("mission: do something cool")
        entry = mock_insert.call_args[0][1]
        assert "do something cool" in entry
        assert "mission:" not in entry.lower()

    @patch("app.command_handlers.insert_pending_mission")
    def test_mission_with_mission_colon_space(self, mock_insert, patch_bridge_state, mock_send):
        from app.command_handlers import handle_mission
        handle_mission("mission : do something")
        entry = mock_insert.call_args[0][1]
        assert "do something" in entry

    @patch("app.command_handlers.insert_pending_mission")
    def test_urgent_mission(self, mock_insert, patch_bridge_state, mock_send):
        from app.command_handlers import handle_mission
        handle_mission("--now fix urgent bug")
        mock_insert.assert_called_once()
        assert mock_insert.call_args[1].get("urgent") or mock_insert.call_args[0][2] is True
        assert "priority" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: _dispatch_skill
# ---------------------------------------------------------------------------

class TestDispatchSkill:
    """Tests for skill dispatch via handle_command."""

    def test_known_skill_dispatched(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import handle_command
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = False
        mock_registry.find_by_command.return_value = skill

        with patch("app.command_handlers.execute_skill", return_value="done") as mock_exec:
            handle_command("/status")
            mock_exec.assert_called_once()
            mock_send.assert_called_once_with("done")

    def test_worker_skill_uses_worker_callback(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        from app.command_handlers import handle_command, set_callbacks
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = True
        mock_registry.find_by_command.return_value = skill

        worker_fn = MagicMock()
        set_callbacks(handle_chat=MagicMock(), run_in_worker=worker_fn)

        with patch("app.command_handlers.execute_skill"):
            handle_command("/review")
            worker_fn.assert_called_once()

    def test_skill_returning_none_does_not_send(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        from app.command_handlers import handle_command
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = False
        mock_registry.find_by_command.return_value = skill

        with patch("app.command_handlers.execute_skill", return_value=None):
            handle_command("/focus")
            mock_send.assert_not_called()

    def test_scoped_command_dispatch(self, patch_bridge_state, mock_send, mock_registry):
        """Test /<scope>.<name> dispatch path."""
        from app.command_handlers import handle_command
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = False
        mock_registry.find_by_command.return_value = None
        mock_registry.resolve_scoped_command.return_value = (skill, "review", "")

        with patch("app.command_handlers.execute_skill", return_value="reviewed") as mock_exec:
            handle_command("/anantys.review")
            mock_exec.assert_called_once()


# ---------------------------------------------------------------------------
# Test: cli_skill dispatch (queue as mission instead of inline execution)
# ---------------------------------------------------------------------------

class TestCliSkillDispatch:
    """Tests for the cli_skill + audience:agent → queue-as-mission path."""

    @patch("app.command_handlers.insert_pending_mission")
    def test_cli_skill_agent_queues_mission(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """A cli_skill skill with audience:agent queues a mission instead of executing inline."""
        from app.command_handlers import _dispatch_skill
        from app.skills import Skill, SkillCommand
        from unittest.mock import patch as _patch

        skill = Skill(
            name="myskill",
            scope="group",
            description="Bridge to my-tool",
            audience="agent",
            cli_skill="my-tool",
            commands=[SkillCommand(name="myskill", description="Invoke /my-tool")],
        )

        with _patch("app.command_handlers.execute_skill") as mock_exec:
            _dispatch_skill(skill, "myskill", "do something")

        # Must NOT call execute_skill (no inline execution)
        mock_exec.assert_not_called()
        # Must queue a mission
        mock_insert.assert_called_once()
        entry = mock_insert.call_args[0][1]
        assert "/group.myskill do something" in entry
        # Should ack to user
        mock_send.assert_called_once()
        assert "Mission queued" in mock_send.call_args[0][0]

    @patch("app.command_handlers.insert_pending_mission")
    def test_cli_skill_agent_extracts_project(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """Project name is extracted from first arg word when it matches a known project."""
        from app.command_handlers import _dispatch_skill
        from app.skills import Skill, SkillCommand
        from unittest.mock import patch as _patch

        skill = Skill(
            name="deploy",
            scope="ops",
            description="Bridge to deploy-tool",
            audience="agent",
            cli_skill="deploy-tool",
            commands=[SkillCommand(name="deploy", description="Invoke /deploy-tool")],
        )

        with _patch("app.command_handlers.execute_skill"), \
             _patch("app.utils.get_known_projects", return_value=[("myproject", "/path")]):
            _dispatch_skill(skill, "deploy", "myproject staging")

        entry = mock_insert.call_args[0][1]
        assert "[project:myproject]" in entry
        assert "/ops.deploy staging" in entry
        assert "project: myproject" in mock_send.call_args[0][0]

    @patch("app.command_handlers.insert_pending_mission")
    def test_cli_skill_agent_extracts_project_case_insensitive(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """Project matching is case-insensitive (e.g. 'KOAN' matches project 'koan')."""
        from app.command_handlers import _queue_cli_skill_mission
        from app.skills import Skill, SkillCommand
        from unittest.mock import patch as _patch

        skill = Skill(
            name="plan",
            scope="core",
            description="Plan",
            audience="agent",
            cli_skill="plan-tool",
            commands=[SkillCommand(name="plan", description="Plan")],
        )

        with _patch("app.utils.get_known_projects", return_value=[("koan", "/path/koan")]):
            _queue_cli_skill_mission(skill, "KOAN fix the login bug")

        entry = mock_insert.call_args[0][1]
        # Must use the canonical name "koan", not "KOAN"
        assert "[project:koan]" in entry
        assert "/core.plan fix the login bug" in entry

    @patch("app.command_handlers.insert_pending_mission")
    def test_cli_skill_without_agent_audience_executes_inline(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """A cli_skill skill WITHOUT audience:agent still executes inline (bridge audience)."""
        from app.command_handlers import _dispatch_skill
        from app.skills import Skill, SkillCommand
        from unittest.mock import patch as _patch

        skill = Skill(
            name="check",
            scope="ops",
            description="Check something",
            audience="bridge",  # NOT agent
            cli_skill="some-tool",
            commands=[SkillCommand(name="check", description="Check")],
        )

        with _patch("app.command_handlers.execute_skill", return_value="done") as mock_exec:
            _dispatch_skill(skill, "check", "args")

        # cli_skill + bridge audience → execute inline, not queue
        mock_exec.assert_called_once()
        mock_insert.assert_not_called()

    @patch("app.command_handlers.insert_pending_mission")
    def test_normal_skill_with_no_cli_skill_executes_inline(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """Skills without cli_skill field are always executed inline."""
        from app.command_handlers import _dispatch_skill
        from app.skills import Skill, SkillCommand
        from unittest.mock import patch as _patch

        skill = Skill(
            name="status",
            scope="core",
            description="Show status",
            audience="agent",
            cli_skill=None,  # no cli_skill
            commands=[SkillCommand(name="status", description="Show status")],
        )

        with _patch("app.command_handlers.execute_skill", return_value="ok") as mock_exec:
            _dispatch_skill(skill, "status", "")

        mock_exec.assert_called_once()
        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# Test: _handle_help
# ---------------------------------------------------------------------------

class TestHandleHelp:
    """Tests for /help L1 grouped output."""

    def test_help_shows_groups(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_help
        mock_registry.groups.return_value = []
        mock_registry.list_all.return_value = []
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "Help" in msg
        assert "missions" in msg
        assert "code" in msg
        assert "status" in msg
        assert "config" in msg
        assert "ideas" in msg
        assert "system" in msg

    def test_help_shows_navigation_hints(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_help
        mock_registry.groups.return_value = []
        mock_registry.list_all.return_value = []
        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/help <group>" in msg
        assert "/help <command>" in msg

    def test_help_shows_non_core_skills_count(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_help
        from app.skills import Skill, SkillCommand

        non_core_skill = MagicMock(spec=Skill)
        non_core_skill.scope = "anantys"
        non_core_skill.description = "Custom review"
        non_core_skill.group = ""
        cmd = MagicMock(spec=SkillCommand)
        cmd.name = "review"
        cmd.description = "Custom review"
        cmd.aliases = []
        non_core_skill.commands = [cmd]
        mock_registry.list_all.return_value = [non_core_skill]
        mock_registry.groups.return_value = []

        _handle_help()
        msg = mock_send.call_args[0][0]
        assert "/skill" in msg
        assert "1 extra skill" in msg

    def test_help_system_shows_core_commands(self, patch_bridge_state, mock_send, mock_registry):
        """L2: /help system must include hardcoded core commands (stop, pause, resume, help, skill)."""
        from app.command_handlers import _handle_help_group
        mock_registry.list_by_group.return_value = []

        _handle_help_group("system", mock_registry)
        msg = mock_send.call_args[0][0]
        assert "/stop" in msg
        assert "/pause" in msg
        assert "/resume" in msg
        assert "/help" in msg
        assert "/skill" in msg

    def test_help_core_command_detail(self, patch_bridge_state, mock_send, mock_registry):
        """L3: /help stop should show details for the hardcoded stop command."""
        from app.command_handlers import _handle_help_detail
        mock_registry.find_by_command.return_value = None

        _handle_help_detail("stop")
        msg = mock_send.call_args[0][0]
        assert "/stop" in msg
        assert "Stop" in msg

    def test_help_core_command_alias(self, patch_bridge_state, mock_send, mock_registry):
        """L3: /help sleep should resolve to the pause core command."""
        from app.command_handlers import _handle_help_detail
        mock_registry.find_by_command.return_value = None

        _handle_help_detail("sleep")
        msg = mock_send.call_args[0][0]
        assert "/pause" in msg
        assert "sleep" in msg.lower()


# ---------------------------------------------------------------------------
# Test: _handle_skill_command
# ---------------------------------------------------------------------------

class TestHandleSkillCommand:
    """Tests for /skill subcommands."""

    def test_skill_list_empty(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("")
        msg = mock_send.call_args[0][0]
        assert "No extra skills loaded" in msg

    def test_skill_list_with_non_core(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        skill.scope = "custom"
        skill.description = "Custom skill"
        cmd = MagicMock(spec=SkillCommand)
        cmd.name = "deploy"
        cmd.description = "Deploy project"
        skill.commands = [cmd]
        mock_registry.list_all.return_value = [skill]
        mock_registry.list_by_scope.return_value = [skill]

        _handle_skill_command("")
        msg = mock_send.call_args[0][0]
        assert "custom" in msg

    @patch("app.skill_manager.install_skill_source", return_value=(True, "Installed!"))
    def test_skill_install(self, mock_install, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("install https://github.com/team/skills.git ops")
        mock_install.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "✅" in msg

    def test_skill_install_no_args(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("install")
        msg = mock_send.call_args[0][0]
        assert "Usage" in msg

    @patch("app.skill_manager.update_all_sources", return_value=(True, "Updated!"))
    def test_skill_update_all(self, mock_update, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("update")
        mock_update.assert_called_once()

    @patch("app.skill_manager.update_skill_source", return_value=(True, "Updated!"))
    def test_skill_update_specific(self, mock_update, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("update ops")
        mock_update.assert_called_once()

    @patch("app.skill_manager.remove_skill_source", return_value=(True, "Removed!"))
    def test_skill_remove(self, mock_remove, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("remove ops")
        mock_remove.assert_called_once()

    def test_skill_remove_no_args(self, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("remove")
        msg = mock_send.call_args[0][0]
        assert "Usage" in msg

    @patch("app.skill_manager.list_sources", return_value="No sources installed.")
    def test_skill_sources(self, mock_list, patch_bridge_state, mock_send, mock_registry):
        from app.command_handlers import _handle_skill_command
        _handle_skill_command("sources")
        mock_list.assert_called_once()

    def test_skill_scope_list(self, patch_bridge_state, mock_send, mock_registry):
        """Test /skill core lists skills in core scope."""
        from app.command_handlers import _handle_skill_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        skill.scope = "core"
        cmd = MagicMock(spec=SkillCommand)
        cmd.name = "status"
        cmd.description = "Show status"
        skill.commands = [cmd]
        mock_registry.list_by_scope.return_value = [skill]

        _handle_skill_command("core")
        msg = mock_send.call_args[0][0]
        assert "core" in msg
        assert "/status" in msg

    def test_skill_scoped_invocation_uses_resolve(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """Test /skill wp.refactor uses resolve_scoped_command for dispatch."""
        from app.command_handlers import _handle_skill_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        skill.worker = False
        skill.cli_skill = None
        skill.audience = "bridge"
        mock_registry.resolve_scoped_command.return_value = (skill, "refactor", "")

        with patch("app.command_handlers.execute_skill", return_value="done"):
            _handle_skill_command("wp.refactor")
        mock_registry.resolve_scoped_command.assert_called_once_with("wp.refactor")

    def test_skill_scoped_invocation_not_found(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """Test /skill wp.nonexistent shows error."""
        from app.command_handlers import _handle_skill_command
        mock_registry.resolve_scoped_command.return_value = None
        _handle_skill_command("wp.nonexistent")
        msg = mock_send.call_args[0][0]
        assert "not found" in msg


# ---------------------------------------------------------------------------
# Test: set_callbacks
# ---------------------------------------------------------------------------

class TestSetCallbacks:
    """Tests for callback injection."""

    def test_set_callbacks_stores_functions(self):
        from app.command_handlers import set_callbacks
        import app.command_handlers as mod
        chat_fn = MagicMock()
        worker_fn = MagicMock()
        set_callbacks(chat_fn, worker_fn)
        assert mod._handle_chat_cb is chat_fn
        assert mod._run_in_worker_cb is worker_fn


# ---------------------------------------------------------------------------
# Test: _reset_session_counters
# ---------------------------------------------------------------------------

class TestResetSessionCounters:
    """Tests for _reset_session_counters."""

    @patch("app.usage_estimator.cmd_reset_session")
    def test_reset_calls_cmd_reset_session(self, mock_reset, patch_bridge_state):
        from app.command_handlers import _reset_session_counters
        _reset_session_counters()
        mock_reset.assert_called_once()

    @patch("app.usage_estimator.cmd_reset_session", side_effect=Exception("boom"))
    def test_reset_handles_exception(self, mock_reset, patch_bridge_state):
        from app.command_handlers import _reset_session_counters
        # Should not raise
        _reset_session_counters()


# ---------------------------------------------------------------------------
# Test: /pause uses pause_manager (not raw file write)
# ---------------------------------------------------------------------------

class TestPauseUsesPauseManager:
    """Tests that /pause creates proper pause state via pause_manager."""

    def test_pause_creates_reason_in_pause_file(self, patch_bridge_state, mock_send):
        """Verify /pause creates .koan-pause with 'manual' reason."""
        from app.command_handlers import handle_command
        handle_command("/pause")
        pause_file = patch_bridge_state / ".koan-pause"
        assert pause_file.exists(), ".koan-pause should exist"
        content = pause_file.read_text()
        assert "manual" in content

    def test_pause_file_contains_display_info(self, patch_bridge_state, mock_send):
        """Verify the pause file contains human-readable display info."""
        from app.command_handlers import handle_command
        handle_command("/pause")
        pause_file = patch_bridge_state / ".koan-pause"
        content = pause_file.read_text()
        assert "Telegram" in content or "paused" in content

    def test_pause_file_has_timestamp(self, patch_bridge_state, mock_send):
        """Verify the pause file contains a valid UNIX timestamp."""
        from app.command_handlers import handle_command
        handle_command("/pause")
        pause_file = patch_bridge_state / ".koan-pause"
        lines = pause_file.read_text().strip().splitlines()
        assert len(lines) >= 2
        timestamp = int(lines[1].strip())
        assert timestamp > 0

    def test_sleep_alias_creates_pause_file_with_reason(self, patch_bridge_state, mock_send):
        """/sleep should also create .koan-pause with reason like /pause."""
        from app.command_handlers import handle_command
        handle_command("/sleep")
        pause_file = patch_bridge_state / ".koan-pause"
        assert pause_file.exists()
        assert "manual" in pause_file.read_text()

    def test_pause_survives_check_and_resume(self, patch_bridge_state, mock_send):
        """Pause created via /pause should NOT be cleaned as orphan."""
        from app.command_handlers import handle_command
        from app.pause_manager import check_and_resume, is_paused
        handle_command("/pause")
        # check_and_resume should NOT remove a manual pause (no auto-resume)
        result = check_and_resume(str(patch_bridge_state))
        assert result is None, "Manual pause should not auto-resume"
        assert is_paused(str(patch_bridge_state)), "Should still be paused"

    def test_already_paused_with_reason_file(self, patch_bridge_state, mock_send):
        """When already paused (with reason file), should report already paused."""
        from app.command_handlers import handle_command
        from app.pause_manager import create_pause
        create_pause(str(patch_bridge_state), "manual")
        handle_command("/pause")
        mock_send.assert_called_once()
        assert "Already paused" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: handle_resume — corrupt and edge-case pause files
# ---------------------------------------------------------------------------

@patch("app.command_handlers._is_runner_alive", return_value=True)
class TestHandleResumeEdgeCases:
    """Tests for handle_resume with corrupt or unusual file contents."""

    @patch("app.command_handlers._reset_session_counters")
    def test_resume_quota_with_empty_timestamp_line(
        self, mock_reset, mock_alive, patch_bridge_state, mock_send
    ):
        """Pause file with empty second line should not crash."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("quota\n\nresets at 10am")
        handle_resume()
        # Should handle gracefully — empty line means no timestamp
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()

    @patch("app.command_handlers._reset_session_counters")
    def test_resume_quota_with_garbage_timestamp(
        self, mock_reset, mock_alive, patch_bridge_state, mock_send
    ):
        """Pause file with non-numeric timestamp should not crash."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("quota\nnot-a-number\nresets")
        handle_resume()
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        # No timestamp → treat as expired → should say "Quota should be reset"
        assert "Quota should be reset" in mock_send.call_args[0][0]

    def test_resume_with_whitespace_in_reason_file(self, mock_alive, patch_bridge_state, mock_send):
        """Pause file with extra whitespace should still parse correctly."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("  manual  \n  \n")
        handle_resume()
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "Unpaused" in mock_send.call_args[0][0]

    def test_resume_with_only_reason_no_timestamp(self, mock_alive, patch_bridge_state, mock_send):
        """Pause file with just the reason, no timestamp line."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("manual")
        handle_resume()
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_send.assert_called_once()
        assert "Unpaused" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: handle_resume — legacy .koan-quota-reset path
# ---------------------------------------------------------------------------

class TestHandleResumeLegacy:
    """Tests for the legacy .koan-quota-reset resume path."""

    def test_legacy_quota_likely_reset(self, patch_bridge_state, mock_send):
        """Legacy quota file with old timestamp → likely reset."""
        from app.command_handlers import handle_resume
        old_ts = int(time.time()) - 10000  # ~2.8 hours ago
        (patch_bridge_state / ".koan-quota-reset").write_text(
            f"resets at 10am\n{old_ts}"
        )
        handle_resume()
        assert not (patch_bridge_state / ".koan-quota-reset").exists()
        mock_send.assert_called_once()
        assert "Quota likely reset" in mock_send.call_args[0][0]

    def test_legacy_quota_not_yet_reset(self, patch_bridge_state, mock_send):
        """Legacy quota file with recent timestamp → not yet reset."""
        from app.command_handlers import handle_resume
        recent_ts = int(time.time()) - 1800  # 30 minutes ago
        (patch_bridge_state / ".koan-quota-reset").write_text(
            f"resets at 10am\n{recent_ts}"
        )
        handle_resume()
        # File should still exist
        assert (patch_bridge_state / ".koan-quota-reset").exists()
        mock_send.assert_called_once()
        assert "not reset yet" in mock_send.call_args[0][0]

    def test_legacy_quota_with_corrupt_timestamp(self, patch_bridge_state, mock_send):
        """Legacy quota file with non-numeric timestamp should not crash."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-quota-reset").write_text(
            "resets at 10am\nnot-a-number"
        )
        handle_resume()
        mock_send.assert_called_once()
        # paused_at defaults to 0 → hours_since_pause is huge → likely_reset
        assert "Quota likely reset" in mock_send.call_args[0][0]

    def test_legacy_quota_with_empty_timestamp(self, patch_bridge_state, mock_send):
        """Legacy quota file with empty second line should not crash."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-quota-reset").write_text("resets at 10am\n")
        handle_resume()
        mock_send.assert_called_once()
        # paused_at defaults to 0 → likely_reset
        assert "Quota likely reset" in mock_send.call_args[0][0]

    def test_legacy_quota_single_line(self, patch_bridge_state, mock_send):
        """Legacy quota file with only reset info, no timestamp."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-quota-reset").write_text("resets at 10am")
        handle_resume()
        mock_send.assert_called_once()
        # paused_at defaults to 0 → likely_reset
        assert "Quota likely reset" in mock_send.call_args[0][0]

    def test_legacy_quota_file_read_error(self, patch_bridge_state, mock_send):
        """Unreadable legacy quota file should not crash."""
        from app.command_handlers import handle_resume
        quota_file = patch_bridge_state / ".koan-quota-reset"
        quota_file.mkdir()  # A directory, not a file → read_text() raises
        handle_resume()
        mock_send.assert_called_once()
        assert "Error" in mock_send.call_args[0][0]


# ---------------------------------------------------------------------------
# Test: handle_mission — auto-detect project from text
# ---------------------------------------------------------------------------

class TestHandleMissionAutoDetect:
    """Tests for handle_mission auto-detecting project from first word."""

    @patch("app.command_handlers.insert_pending_mission")
    @patch("app.command_handlers.detect_project_from_text",
           return_value=("koan", "fix the login bug"))
    @patch("app.command_handlers._parse_project", return_value=(None, "koan fix the login bug"))
    def test_auto_detect_project_from_first_word(
        self, mock_parse, mock_detect, mock_insert, patch_bridge_state, mock_send
    ):
        """'koan fix the login bug' auto-detects project 'koan'."""
        from app.command_handlers import handle_mission
        handle_mission("koan fix the login bug")
        entry = mock_insert.call_args[0][1]
        assert "[project:koan]" in entry
        assert "fix the login bug" in entry
        assert "project: koan" in mock_send.call_args[0][0]

    @patch("app.command_handlers.insert_pending_mission")
    @patch("app.command_handlers.detect_project_from_text",
           return_value=(None, "fix something random"))
    @patch("app.command_handlers._parse_project", return_value=(None, "fix something random"))
    def test_no_auto_detect_when_first_word_not_project(
        self, mock_parse, mock_detect, mock_insert, patch_bridge_state, mock_send
    ):
        """When first word isn't a known project, no project tag added."""
        from app.command_handlers import handle_mission
        handle_mission("fix something random")
        entry = mock_insert.call_args[0][1]
        assert "[project:" not in entry

    @patch("app.command_handlers.insert_pending_mission")
    @patch("app.command_handlers.detect_project_from_text")
    @patch("app.command_handlers._parse_project",
           return_value=("backend", "fix it"))
    def test_explicit_project_tag_takes_priority(
        self, mock_parse, mock_detect, mock_insert, patch_bridge_state, mock_send
    ):
        """Explicit [project:X] tag takes priority — auto-detect not called."""
        from app.command_handlers import handle_mission
        handle_mission("[project:backend] fix it")
        mock_detect.assert_not_called()
        entry = mock_insert.call_args[0][1]
        assert "[project:backend]" in entry


# ---------------------------------------------------------------------------
# Test: handle_command — edge cases
# ---------------------------------------------------------------------------

class TestHandleCommandEdgeCases:
    """Edge cases for the main handle_command dispatch."""

    def test_slash_only_sends_unknown(self, patch_bridge_state, mock_send, mock_registry):
        """Just '/' should report unknown command, not crash."""
        from app.command_handlers import handle_command
        handle_command("/")
        mock_send.assert_called_once()
        assert "Unknown command" in mock_send.call_args[0][0]

    def test_command_with_extra_whitespace(self, patch_bridge_state, mock_send):
        """'/stop  ' (trailing spaces) should still match /stop."""
        from app.command_handlers import handle_command
        handle_command("/stop  ")
        assert (patch_bridge_state / ".koan-stop").exists()

    def test_command_case_insensitive(self, patch_bridge_state, mock_send):
        """'/STOP' should match /stop."""
        from app.command_handlers import handle_command
        handle_command("/STOP")
        assert (patch_bridge_state / ".koan-stop").exists()

    def test_help_with_slash_prefix(self, patch_bridge_state, mock_send, mock_registry):
        """/help /mission should look up 'mission', stripping the extra /."""
        from app.command_handlers import handle_command
        # _handle_help_detail strips / and checks groups first, then commands
        mock_registry.find_by_command.return_value = None
        handle_command("/help /mission")
        mock_registry.find_by_command.assert_called_with("mission")


# ---------------------------------------------------------------------------
# Test: _dispatch_skill — worker with no callback
# ---------------------------------------------------------------------------

class TestDispatchSkillWorkerNoCallback:
    """Tests for worker skill dispatch when callback is not set."""

    def test_worker_skill_without_callback_sends_error(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """Worker skill with _run_in_worker_cb=None should notify user."""
        from app.command_handlers import _dispatch_skill, set_callbacks
        from app.skills import Skill
        import app.command_handlers as mod

        skill = MagicMock(spec=Skill)
        skill.worker = True
        skill.cli_skill = None
        skill.audience = "bridge"

        # Set worker callback to None
        old_cb = mod._run_in_worker_cb
        mod._run_in_worker_cb = None
        try:
            with patch("app.command_handlers.execute_skill", return_value="result"):
                _dispatch_skill(skill, "sparring", "")
            # Should send an error message instead of silently dropping
            mock_send.assert_called_once()
            assert "worker thread not available" in mock_send.call_args[0][0]
        finally:
            mod._run_in_worker_cb = old_cb

    def test_worker_skill_sends_empty_string_result(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """Worker skill returning empty string '' should still send it (not drop)."""
        from app.command_handlers import _dispatch_skill
        from app.skills import Skill
        import app.command_handlers as mod

        skill = MagicMock(spec=Skill)
        skill.worker = True
        skill.cli_skill = None
        skill.audience = "bridge"

        # Use a real function as worker callback that runs immediately
        def run_immediately(fn):
            fn()

        old_cb = mod._run_in_worker_cb
        mod._run_in_worker_cb = run_immediately
        try:
            with patch("app.command_handlers.execute_skill", return_value=""):
                _dispatch_skill(skill, "check", "")
            # Empty string is a valid result — must be sent, not dropped
            mock_send.assert_called_once_with("")
        finally:
            mod._run_in_worker_cb = old_cb


# ---------------------------------------------------------------------------
# Test: _dispatch_skill — worker exception handling
# ---------------------------------------------------------------------------

class TestDispatchSkillWorkerExceptionHandling:
    """Tests for worker skill exception handling in _dispatch_skill."""

    def test_worker_skill_exception_is_logged(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """Worker skill that raises should log and notify the user."""
        from app.command_handlers import _dispatch_skill, set_callbacks
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = True
        skill.cli_skill = None
        skill.audience = "bridge"

        # Capture the closure and run it synchronously
        captured_fn = None
        def capture_worker(fn):
            nonlocal captured_fn
            captured_fn = fn
        set_callbacks(handle_chat=MagicMock(), run_in_worker=capture_worker)

        with patch("app.command_handlers.execute_skill", side_effect=RuntimeError("skill crashed")), \
             patch("app.command_handlers.log") as mock_log:
            _dispatch_skill(skill, "sparring", "")
            assert captured_fn is not None
            captured_fn()
            mock_log.assert_any_call("error", "Worker skill 'sparring' failed: skill crashed")
        # Should also notify user via Telegram
        assert any("sparring" in str(c) and "failed" in str(c) for c in mock_send.call_args_list)

    def test_worker_skill_exception_notification_failure(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """If notification also fails after worker exception, don't crash."""
        from app.command_handlers import _dispatch_skill, set_callbacks
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = True
        skill.cli_skill = None
        skill.audience = "bridge"

        captured_fn = None
        def capture_worker(fn):
            nonlocal captured_fn
            captured_fn = fn
        set_callbacks(handle_chat=MagicMock(), run_in_worker=capture_worker)

        # Make send_telegram also fail
        mock_send.side_effect = ConnectionError("network down")
        with patch("app.command_handlers.execute_skill", side_effect=ValueError("bad")), \
             patch("app.command_handlers.log"):
            _dispatch_skill(skill, "review", "")
            assert captured_fn is not None
            # Should not raise despite double failure
            captured_fn()

    def test_worker_skill_send_telegram_exception(
        self, patch_bridge_state, mock_send, mock_registry
    ):
        """If send_telegram raises in worker (after execute_skill succeeds), catch it."""
        from app.command_handlers import _dispatch_skill, set_callbacks
        from app.skills import Skill

        skill = MagicMock(spec=Skill)
        skill.worker = True
        skill.cli_skill = None
        skill.audience = "bridge"

        captured_fn = None
        def capture_worker(fn):
            nonlocal captured_fn
            captured_fn = fn
        set_callbacks(handle_chat=MagicMock(), run_in_worker=capture_worker)

        # send_telegram will raise on the first call (sending result)
        mock_send.side_effect = ConnectionError("send failed")
        with patch("app.command_handlers.execute_skill", return_value="result text"), \
             patch("app.command_handlers.log") as mock_log:
            _dispatch_skill(skill, "magic", "")
            assert captured_fn is not None
            captured_fn()
            mock_log.assert_any_call("error", "Worker skill 'magic' failed: send failed")


# ---------------------------------------------------------------------------
# Test: _handle_help_command — detailed help with aliases and usage
# ---------------------------------------------------------------------------

class TestHandleHelpCommandDetail:
    """Tests for /help <command> with various skill configurations."""

    def test_help_shows_aliases(self, patch_bridge_state, mock_send, mock_registry):
        """Help for a command with aliases should list them."""
        from app.command_handlers import _handle_help_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(
            name="cancel",
            description="Cancel a mission",
            aliases=["remove", "clear"],
            usage="/cancel [number|keyword]",
        )
        skill.commands = [cmd]
        skill.description = "Cancel"
        mock_registry.find_by_command.return_value = skill

        _handle_help_command("cancel")
        msg = mock_send.call_args[0][0]
        assert "/cancel" in msg
        assert "remove" in msg
        assert "clear" in msg
        assert "/cancel [number|keyword]" in msg

    def test_help_shows_no_usage_defined(self, patch_bridge_state, mock_send, mock_registry):
        """Help for a command without usage should say 'No usage defined.'."""
        from app.command_handlers import _handle_help_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(name="ping", description="Check status")
        skill.commands = [cmd]
        skill.description = "Ping"
        mock_registry.find_by_command.return_value = skill

        _handle_help_command("ping")
        msg = mock_send.call_args[0][0]
        assert "No usage defined" in msg

    def test_help_unknown_command(self, patch_bridge_state, mock_send, mock_registry):
        """/help nonexistent should report unknown."""
        from app.command_handlers import _handle_help_command
        mock_registry.find_by_command.return_value = None
        _handle_help_command("nonexistent")
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg

    def test_help_matches_by_alias(self, patch_bridge_state, mock_send, mock_registry):
        """Help for an alias should still find the right command."""
        from app.command_handlers import _handle_help_command
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(
            name="cancel",
            description="Cancel a mission",
            aliases=["remove"],
        )
        skill.commands = [cmd]
        skill.description = "Cancel"
        mock_registry.find_by_command.return_value = skill

        _handle_help_command("remove")
        msg = mock_send.call_args[0][0]
        assert "/cancel" in msg
        assert "Cancel a mission" in msg


# ---------------------------------------------------------------------------
# Test: _handle_help_detail — L2 group expansion
# ---------------------------------------------------------------------------

class TestHandleHelpGroup:
    """Tests for /help <group> — L2 group expansion."""

    def test_help_group_shows_commands(self, patch_bridge_state, mock_send, mock_registry):
        """L2: /help missions should list commands in the missions group."""
        from app.command_handlers import _handle_help_detail
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        skill.scope = "core"
        skill.group = "missions"
        cmd = SkillCommand(name="mission", description="Create a mission", aliases=[])
        skill.commands = [cmd]
        skill.description = "Create a mission"
        mock_registry.list_by_group.return_value = [skill]

        _handle_help_detail("missions")
        msg = mock_send.call_args[0][0]
        assert "Missions" in msg
        assert "/mission" in msg
        assert "Create a mission" in msg

    def test_help_group_shows_aliases(self, patch_bridge_state, mock_send, mock_registry):
        """L2: commands with aliases should display them."""
        from app.command_handlers import _handle_help_detail
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        skill.scope = "core"
        skill.group = "missions"
        cmd = SkillCommand(name="list", description="View the queue", aliases=["queue", "ls"])
        skill.commands = [cmd]
        skill.description = "View the queue"
        mock_registry.list_by_group.return_value = [skill]

        _handle_help_detail("missions")
        msg = mock_send.call_args[0][0]
        assert "/list" in msg
        assert "queue" in msg
        assert "ls" in msg

    def test_help_group_falls_through_to_command(self, patch_bridge_state, mock_send, mock_registry):
        """If arg is not a group, try as a command (L3)."""
        from app.command_handlers import _handle_help_detail
        from app.skills import Skill, SkillCommand

        skill = MagicMock(spec=Skill)
        cmd = SkillCommand(name="status", description="Show status")
        skill.commands = [cmd]
        skill.description = "Show status"
        mock_registry.find_by_command.return_value = skill

        _handle_help_detail("status")
        msg = mock_send.call_args[0][0]
        # "status" IS a group name, so it shows the group view, not L3
        assert "Status" in msg

    def test_help_unknown_suggests(self, patch_bridge_state, mock_send, mock_registry):
        """Unknown arg that matches no group or command suggests closest."""
        from app.command_handlers import _handle_help_detail
        mock_registry.find_by_command.return_value = None
        mock_registry.suggest_command.return_value = "missions"

        _handle_help_detail("missons")  # typo
        msg = mock_send.call_args[0][0]
        assert "Unknown command" in msg
        assert "missions" in msg

    def test_help_via_handle_command(self, patch_bridge_state, mock_send, mock_registry):
        """/help code should expand the code group."""
        from app.command_handlers import handle_command
        mock_registry.list_by_group.return_value = []

        handle_command("/help code")
        msg = mock_send.call_args[0][0]
        assert "Code" in msg


# ---------------------------------------------------------------------------
# Test: _queue_cli_skill_mission — edge cases
# ---------------------------------------------------------------------------

class TestQueueCliSkillMissionEdgeCases:
    """Edge cases for cli_skill mission queuing."""

    @patch("app.command_handlers.insert_pending_mission")
    def test_queue_with_empty_args(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """CLI skill with no args should queue just the command."""
        from app.command_handlers import _queue_cli_skill_mission
        from app.skills import Skill, SkillCommand

        skill = Skill(
            name="deploy",
            scope="ops",
            description="Deploy",
            audience="agent",
            cli_skill="deploy-tool",
            commands=[SkillCommand(name="deploy", description="Deploy")],
        )

        _queue_cli_skill_mission(skill, "")
        entry = mock_insert.call_args[0][1]
        assert entry == "- /ops.deploy"
        assert "[project:" not in entry

    @patch("app.command_handlers.insert_pending_mission")
    @patch("app.utils.get_known_projects", return_value=[])
    def test_queue_with_no_known_projects(
        self, mock_projects, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """When no projects are configured, first word is treated as args."""
        from app.command_handlers import _queue_cli_skill_mission
        from app.skills import Skill, SkillCommand

        skill = Skill(
            name="check",
            scope="ops",
            description="Check",
            audience="agent",
            cli_skill="check-tool",
            commands=[SkillCommand(name="check", description="Check")],
        )

        _queue_cli_skill_mission(skill, "myarg something")
        entry = mock_insert.call_args[0][1]
        assert "/ops.check myarg something" in entry
        assert "[project:" not in entry

    @patch("app.command_handlers.insert_pending_mission")
    def test_queue_truncates_long_args_in_ack(
        self, mock_insert, patch_bridge_state, mock_send, mock_registry
    ):
        """Acknowledgment message truncates very long args."""
        from app.command_handlers import _queue_cli_skill_mission
        from app.skills import Skill, SkillCommand

        skill = Skill(
            name="plan",
            scope="core",
            description="Plan",
            audience="agent",
            cli_skill="plan-tool",
            commands=[SkillCommand(name="plan", description="Plan")],
        )

        long_args = "x" * 1000
        _queue_cli_skill_mission(skill, long_args)
        msg = mock_send.call_args[0][0]
        # The koan_cmd[:500] truncation should apply
        assert len(msg) < 1500


# ---------------------------------------------------------------------------
# Test: handle_resume — auto-restart dead runner
# ---------------------------------------------------------------------------

class TestHandleResumeAutoRestart:
    """Tests for handle_resume auto-restarting a dead runner."""

    @patch("app.command_handlers._is_runner_alive", return_value=False)
    @patch("app.command_handlers._auto_restart_runner", return_value=True)
    def test_resume_paused_with_dead_runner_restarts(
        self, mock_restart, mock_alive, patch_bridge_state, mock_send
    ):
        """When paused and runner is dead, resume should auto-restart."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("manual\n")
        handle_resume()
        # Pause removed + message sent + restart triggered
        assert not (patch_bridge_state / ".koan-pause").exists()
        mock_restart.assert_called_once()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    @patch("app.command_handlers._auto_restart_runner")
    def test_resume_paused_with_alive_runner_no_restart(
        self, mock_restart, mock_alive, patch_bridge_state, mock_send
    ):
        """When paused and runner is alive, resume should NOT restart."""
        from app.command_handlers import handle_resume
        (patch_bridge_state / ".koan-pause").write_text("manual\n")
        handle_resume()
        mock_restart.assert_not_called()

    @patch("app.command_handlers._is_runner_alive", return_value=False)
    @patch("app.command_handlers._auto_restart_runner", return_value=True)
    def test_resume_no_pause_no_quota_dead_runner_restarts(
        self, mock_restart, mock_alive, patch_bridge_state, mock_send
    ):
        """When no pause file and runner is dead, resume should restart."""
        from app.command_handlers import handle_resume
        handle_resume()
        mock_restart.assert_called_once()

    @patch("app.command_handlers._is_runner_alive", return_value=True)
    def test_resume_no_pause_alive_runner_shows_info(
        self, mock_alive, patch_bridge_state, mock_send
    ):
        """When no pause and runner is alive, show info message."""
        from app.command_handlers import handle_resume
        handle_resume()
        mock_send.assert_called_once()
        assert "No pause" in mock_send.call_args[0][0]

    @patch("app.pid_manager.check_pidfile", return_value=None)
    @patch("app.pid_manager.start_runner", return_value=(True, "Agent loop started (PID 999)"))
    def test_auto_restart_runner_calls_start_runner_with_skip_env(
        self, mock_start, mock_check, patch_bridge_state, mock_send
    ):
        """_auto_restart_runner should pass KOAN_SKIP_START_PAUSE=1."""
        from app.command_handlers import _auto_restart_runner
        result = _auto_restart_runner()
        assert result is True
        mock_start.assert_called_once()
        extra_env = mock_start.call_args[1].get("extra_env", {})
        assert extra_env.get("KOAN_SKIP_START_PAUSE") == "1"
        assert "restarting" in mock_send.call_args[0][0].lower()

    @patch("app.pid_manager.check_pidfile", return_value=None)
    @patch("app.pid_manager.start_runner", return_value=(False, "Failed to launch"))
    def test_auto_restart_runner_failure_sends_error(
        self, mock_start, mock_check, patch_bridge_state, mock_send
    ):
        """_auto_restart_runner should report failure."""
        from app.command_handlers import _auto_restart_runner
        result = _auto_restart_runner()
        assert result is False
        assert "Failed" in mock_send.call_args[0][0]
