from pathlib import Path

import pytest


@pytest.fixture
def api_spec_path():
    return Path(__file__).resolve().parents[1] / "openapi.yaml"
