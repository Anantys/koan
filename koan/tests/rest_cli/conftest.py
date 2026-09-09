from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app.cli.spec import load_operations


class FakeResponse:
    def __init__(self, status, payload=None, *, text=""):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def response_factory():
    return FakeResponse


@pytest.fixture
def session_factory():
    return FakeSession


@pytest.fixture
def api_spec_path() -> Path:
    return Path(__file__).resolve().parents[2] / "openapi.yaml"


@pytest.fixture
def enriched_operations(api_spec_path, tmp_path):
    spec = yaml.safe_load(api_spec_path.read_text())
    enriched = deepcopy(spec)
    enriched["paths"]["/v1/logs"]["get"]["parameters"] = [
        {
            "name": "cursor",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    enriched["paths"]["/v1/missions"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": {"type": "string"},
                        "urgent": {"type": "boolean"},
                    },
                }
            }
        },
    }
    path = tmp_path / "enriched-openapi.yaml"
    path.write_text(yaml.safe_dump(enriched, sort_keys=True))
    return load_operations(enriched)
