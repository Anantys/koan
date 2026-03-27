"""Tests for module-level prompt imports in CLI runner modules.

When a CLI runner subprocess operates on the koan repo itself, git checkouts
can change the Python source files on disk.  If prompts functions are imported
lazily (inside function bodies), the import may resolve against a different
branch's code.  Moving imports to module level ensures they are bound at
subprocess start time, before any git checkout can change the working tree.

This test suite verifies:
1. All CLI runner modules import load_skill_prompt at module level
2. No CLI runner has lazy (function-body) imports of prompt functions
3. The imported names are usable (not stale or missing)
"""

import ast
import importlib
import sys
from pathlib import Path

import pytest


# CLI runner modules that run as subprocesses and may do git checkouts
# on the project (which could be the koan repo itself).
CLI_RUNNER_MODULES = [
    "app.recreate_pr",
    "app.rebase_pr",
    "app.review_runner",
    "app.plan_runner",
    "app.ai_runner",
    "app.claudemd_refresh",
    "app.pr_review",
    "skills.core.implement.implement_runner",
    "skills.core.fix.fix_runner",
]


class TestModuleLevelPromptImports:
    """Verify prompt functions are imported at module level, not lazily."""

    @pytest.mark.parametrize("module_name", CLI_RUNNER_MODULES)
    def test_prompt_loader_at_module_level(self, module_name):
        """Each CLI runner must import a prompt loader at module level."""
        mod = importlib.import_module(module_name)
        has_loader = (
            hasattr(mod, "load_skill_prompt")
            or hasattr(mod, "load_prompt_or_skill")
        )
        assert has_loader, (
            f"{module_name} must import load_skill_prompt or "
            f"load_prompt_or_skill at module level to avoid ImportError "
            f"when the working tree changes during git checkout "
            f"(e.g. when project_path is the koan repo)."
        )

    @pytest.mark.parametrize("module_name", CLI_RUNNER_MODULES)
    def test_no_lazy_prompt_imports(self, module_name):
        """No lazy 'from app.prompts import ...' inside function bodies."""
        mod = importlib.import_module(module_name)
        source_file = Path(mod.__file__)
        tree = ast.parse(source_file.read_text())

        lazy_imports = _find_lazy_prompt_imports(tree)
        assert not lazy_imports, (
            f"{module_name} has lazy prompt imports inside functions at "
            f"line(s) {lazy_imports}. Move them to module level."
        )

    @pytest.mark.parametrize("module_name", CLI_RUNNER_MODULES)
    def test_imported_functions_are_callable(self, module_name):
        """The imported prompt functions must be the real functions."""
        mod = importlib.import_module(module_name)
        from app import prompts as prompts_mod

        # Check whichever loader the module uses
        for attr in ("load_prompt_or_skill", "load_skill_prompt"):
            fn = getattr(mod, attr, None)
            if fn is not None:
                assert callable(fn), f"{module_name}.{attr} is not callable"
                original = getattr(prompts_mod, attr)
                assert fn is original, (
                    f"{module_name}.{attr} is not the same object as "
                    f"app.prompts.{attr} — possible stale import"
                )
                return

        pytest.fail(
            f"{module_name} has neither load_prompt_or_skill nor "
            f"load_skill_prompt at module level"
        )


def _find_lazy_prompt_imports(tree: ast.AST) -> list:
    """Find 'from app.prompts import ...' inside function bodies.

    Returns a list of line numbers where lazy imports were found.
    """
    lazy_lines = []

    class LazyImportFinder(ast.NodeVisitor):
        def __init__(self):
            self._in_function = False

        def visit_FunctionDef(self, node):
            old = self._in_function
            self._in_function = True
            self.generic_visit(node)
            self._in_function = old

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node):
            if (
                self._in_function
                and node.module == "app.prompts"
                and any(
                    alias.name in (
                        "load_skill_prompt", "load_prompt",
                        "load_prompt_or_skill",
                    )
                    for alias in node.names
                )
            ):
                lazy_lines.append(node.lineno)

    LazyImportFinder().visit(tree)
    return lazy_lines


class TestBuildPromptFunctions:
    """Test that prompt-building functions work with both skill_dir and without."""

    @pytest.fixture
    def pr_context(self):
        return {
            "title": "feat: test feature",
            "body": "Test body",
            "branch": "koan/test-branch",
            "base": "main",
            "diff": "+some changes",
            "review_comments": "looks fine",
            "reviews": "APPROVED",
            "issue_comments": "please fix",
        }

    @pytest.fixture
    def recreate_skill_dir(self):
        return Path(__file__).parent.parent / "skills" / "core" / "recreate"

    @pytest.fixture
    def rebase_skill_dir(self):
        return Path(__file__).parent.parent / "skills" / "core" / "rebase"

    def test_recreate_prompt_with_skill_dir(self, pr_context, recreate_skill_dir):
        """_build_recreate_prompt works with skill_dir using module-level import."""
        from app.recreate_pr import _build_recreate_prompt
        prompt = _build_recreate_prompt(pr_context, skill_dir=recreate_skill_dir)
        assert "test feature" in prompt
        assert "koan/test-branch" in prompt

    def test_recreate_prompt_without_skill_dir(self, pr_context):
        """_build_recreate_prompt falls back to load_prompt without skill_dir."""
        from unittest.mock import patch
        from app.recreate_pr import _build_recreate_prompt
        with patch("app.claude_step.load_prompt_or_skill", return_value="fallback") as mock:
            result = _build_recreate_prompt(pr_context, skill_dir=None)
            mock.assert_called_once()
            assert result == "fallback"

    def test_rebase_prompt_with_skill_dir(self, pr_context, rebase_skill_dir):
        """_build_rebase_prompt works with skill_dir using module-level import."""
        from app.rebase_pr import _build_rebase_prompt
        prompt = _build_rebase_prompt(pr_context, skill_dir=rebase_skill_dir)
        assert "test feature" in prompt
        assert "koan/test-branch" in prompt

    def test_rebase_prompt_without_skill_dir(self, pr_context):
        """_build_rebase_prompt falls back to load_prompt without skill_dir."""
        from unittest.mock import patch
        from app.rebase_pr import _build_rebase_prompt
        with patch("app.claude_step.load_prompt_or_skill", return_value="fallback") as mock:
            result = _build_rebase_prompt(pr_context, skill_dir=None)
            mock.assert_called_once()
            assert result == "fallback"
