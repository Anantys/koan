"""Tests for app.preflight — pre-flight quota check module."""

import pytest
from unittest.mock import MagicMock, patch


class TestPreflightQuotaCheck:
    """Tests for preflight_quota_check()."""

    # Note: preflight.py uses lazy imports inside the function body:
    #   from app.usage_tracker import _get_budget_mode
    #   from app.provider import get_provider
    # We must patch at the SOURCE module (app.usage_tracker, app.provider),
    # not at app.preflight.

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_quota_available(self, mock_budget, mock_get_prov):
        """When provider says quota is available, returns (True, None)."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (True, "")
        mock_get_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is True
        assert error is None
        provider.check_quota_available.assert_called_once_with("/tmp/proj")

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_quota_exhausted(self, mock_budget, mock_get_prov):
        """When provider says quota is exhausted, returns (False, detail)."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (False, "Rate limit exceeded")
        mock_get_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is False
        assert error == "Rate limit exceeded"

    @patch("app.usage_tracker._get_budget_mode", return_value="disabled")
    def test_budget_disabled_skips_check(self, mock_budget):
        """When budget_mode is 'disabled', skip the check entirely."""
        from app.preflight import preflight_quota_check

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is True
        assert error is None

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="session_only")
    def test_budget_session_only_still_checks(self, mock_budget, mock_get_prov):
        """When budget_mode is 'session_only', still run the preflight check."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (True, "")
        mock_get_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is True
        provider.check_quota_available.assert_called_once()

    @patch("app.provider.get_provider", side_effect=Exception("provider broken"))
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_provider_error_proceeds_optimistically(self, mock_budget, mock_prov):
        """If get_provider() raises, proceed optimistically (True, None)."""
        from app.preflight import preflight_quota_check

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is True
        assert error is None

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", side_effect=ImportError("no module"))
    def test_budget_mode_import_error_proceeds(self, mock_budget, mock_prov):
        """If _get_budget_mode import fails, skip check and continue to provider."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (True, "")
        mock_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is True

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_passes_project_path_to_provider(self, mock_budget, mock_prov):
        """Verify the project_path argument is forwarded to the provider."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (True, "")
        mock_prov.return_value = provider

        preflight_quota_check("/my/special/path", "/inst")
        provider.check_quota_available.assert_called_once_with("/my/special/path")

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_quota_exhausted_with_empty_detail(self, mock_budget, mock_prov):
        """Quota exhausted with empty error detail still returns False."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (False, "")
        mock_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/instance")
        assert ok is False
        assert error == ""

    @patch("app.provider.get_provider")
    @patch("app.usage_tracker._get_budget_mode", return_value="full")
    def test_project_name_accepted(self, mock_budget, mock_prov):
        """project_name parameter is accepted (for future per-project providers)."""
        from app.preflight import preflight_quota_check

        provider = MagicMock()
        provider.check_quota_available.return_value = (True, "")
        mock_prov.return_value = provider

        ok, error = preflight_quota_check("/tmp/proj", "/tmp/inst", project_name="myproject")
        assert ok is True


class TestPreflightModuleStructure:
    """Verify module imports and structure."""

    def test_module_imports_cleanly(self):
        """preflight.py should import without side effects."""
        import importlib
        mod = importlib.import_module("app.preflight")
        assert hasattr(mod, "preflight_quota_check")

    def test_function_signature(self):
        """Check the function has expected parameters."""
        import inspect
        from app.preflight import preflight_quota_check
        sig = inspect.signature(preflight_quota_check)
        params = list(sig.parameters.keys())
        assert "project_path" in params
        assert "instance_dir" in params
        assert "project_name" in params

    def test_return_type_annotation(self):
        """Function has proper return type annotation."""
        import inspect
        from app.preflight import preflight_quota_check
        sig = inspect.signature(preflight_quota_check)
        # Return annotation should be Tuple[bool, Optional[str]]
        assert sig.return_annotation is not inspect.Parameter.empty
