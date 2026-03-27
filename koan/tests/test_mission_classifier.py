"""Tests for mission_classifier.py — mission type classification."""

import pytest

from app.mission_classifier import classify_mission


class TestClassifyMission:
    """Tests for classify_mission()."""

    # --- Debug type ---

    def test_fix_keyword(self):
        assert classify_mission("fix: auth token refresh fails after 24h") == "debug"

    def test_bug_keyword(self):
        assert classify_mission("bug in pagination when page > 100") == "debug"

    def test_debug_keyword(self):
        assert classify_mission("debug the flaky test in CI") == "debug"

    def test_crash_keyword(self):
        assert classify_mission("crash on startup with empty config") == "debug"

    def test_french_corriger(self):
        assert classify_mission("corriger le bug d'authentification") == "debug"

    # --- Implement type ---

    def test_implement_keyword(self):
        assert classify_mission("implement webhook support") == "implement"

    def test_add_keyword(self):
        assert classify_mission("add dark mode toggle") == "implement"

    def test_create_keyword(self):
        assert classify_mission("create a new CLI command for export") == "implement"

    def test_feature_keyword(self):
        assert classify_mission("feature: multi-project dashboard") == "implement"

    def test_french_ajouter(self):
        assert classify_mission("ajouter le support multi-langue") == "implement"

    # --- Design type ---

    def test_design_keyword(self):
        assert classify_mission("design the plugin architecture") == "design"

    def test_plan_keyword(self):
        assert classify_mission("plan the migration to async") == "design"

    def test_rfc_keyword(self):
        assert classify_mission("RFC: new config format") == "design"

    def test_french_concevoir(self):
        assert classify_mission("concevoir l'architecture des hooks") == "design"

    # --- Review type ---

    def test_review_keyword(self):
        assert classify_mission("review the auth middleware") == "review"

    def test_audit_keyword(self):
        assert classify_mission("audit security of API endpoints") == "review"

    def test_french_auditer(self):
        assert classify_mission("auditer les dépendances") == "review"

    # --- Refactor type ---

    def test_refactor_keyword(self):
        assert classify_mission("refactor the notification system") == "refactor"

    def test_cleanup_keyword(self):
        assert classify_mission("clean up the test fixtures") == "refactor"

    def test_simplify_keyword(self):
        assert classify_mission("simplify the config loading") == "refactor"

    def test_french_refactoriser(self):
        assert classify_mission("refactoriser le module utils") == "refactor"

    # --- Docs type ---

    def test_document_keyword(self):
        assert classify_mission("document the API endpoints") == "docs"

    def test_readme_keyword(self):
        assert classify_mission("update README for the project") == "docs"

    def test_french_documenter(self):
        assert classify_mission("documenter le système de skills") == "docs"

    # --- General type (no match) ---

    def test_empty_string(self):
        assert classify_mission("") == "general"

    def test_whitespace_only(self):
        assert classify_mission("   ") == "general"

    def test_no_keywords(self):
        assert classify_mission("migrate to Python 3.12") == "general"

    # --- Priority ordering ---

    def test_fix_beats_implement(self):
        """'fix the implementation' is debug, not implement."""
        assert classify_mission("fix the implementation of auth") == "debug"

    def test_review_beats_implement(self):
        """'review the new feature' is review, not implement."""
        assert classify_mission("review the new feature") == "review"

    def test_fix_beats_design(self):
        """'fix the design doc' is debug, not design."""
        assert classify_mission("fix the design document") == "debug"

    # --- Edge cases ---

    def test_multiline_uses_first_line(self):
        title = "fix auth bug\nthis has more details\nand even more"
        assert classify_mission(title) == "debug"

    def test_strips_list_prefix(self):
        assert classify_mission("- fix the login flow") == "debug"

    def test_strips_project_tag(self):
        assert classify_mission("[project:myapp] implement SSO") == "implement"

    def test_projet_tag_variant(self):
        assert classify_mission("[projet:myapp] ajouter le cache") == "implement"

    def test_case_insensitive(self):
        assert classify_mission("FIX the broken tests") == "debug"

    def test_project_tag_only(self):
        """Project tag with no remaining text → general."""
        assert classify_mission("[project:foo]   ") == "general"
