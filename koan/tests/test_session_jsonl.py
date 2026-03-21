"""Tests for koan/app/session_jsonl.py — JSONL session file parsing."""

import json

import pytest

from app.session_jsonl import (
    _encode_project_path,
    collect_jsonl_tokens,
    find_session_jsonl,
    parse_session_tail,
    read_tail_bytes,
)


class TestEncodeProjectPath:
    def test_slashes_replaced_with_dashes(self):
        assert _encode_project_path("/Users/foo/project") == "-Users-foo-project"

    def test_no_slashes(self):
        assert _encode_project_path("project") == "project"


class TestFindSessionJsonl:
    def test_found(self, tmp_path, monkeypatch):
        project_path = "/Users/foo/myproject"
        encoded = "-Users-foo-myproject"
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)

        old = proj_dir / "old-session.jsonl"
        old.write_text('{"sessionId":"old"}\n')

        new = proj_dir / "new-session.jsonl"
        new.write_text('{"sessionId":"new"}\n')
        # Ensure new is more recent
        import os
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = find_session_jsonl(project_path)
        assert result == new

    def test_missing_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert find_session_jsonl("/nonexistent/path") is None

    def test_empty_directory(self, tmp_path, monkeypatch):
        encoded = "-Users-foo-empty"
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert find_session_jsonl("/Users/foo/empty") is None


class TestReadTailBytes:
    def test_small_file_read_fully(self, tmp_path):
        f = tmp_path / "small.jsonl"
        content = b'{"line":1}\n{"line":2}\n'
        f.write_bytes(content)
        assert read_tail_bytes(f) == content

    def test_large_file_reads_tail(self, tmp_path):
        f = tmp_path / "large.jsonl"
        # Write more than max_bytes
        content = b"x" * 1000
        f.write_bytes(content)
        result = read_tail_bytes(f, max_bytes=100)
        assert len(result) == 100
        assert result == b"x" * 100

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nope.jsonl"
        assert read_tail_bytes(f) == b""

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_bytes(b"")
        assert read_tail_bytes(f) == b""


class TestParseSessionTail:
    def test_valid_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"sessionId": "abc-123", "inputTokens": 100, "outputTokens": 50}),
            json.dumps({"inputTokens": 200, "outputTokens": 75, "costUSD": 0.005}),
            json.dumps({"inputTokens": 150, "outputTokens": 60, "costUSD": 0.012}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_session_tail(f)
        assert result["session_id"] == "abc-123"
        assert result["input_tokens"] == 450
        assert result["output_tokens"] == 185
        # Last costUSD seen
        assert result["cost_usd"] == 0.012

    def test_truncated_head_skipped(self, tmp_path):
        f = tmp_path / "truncated.jsonl"
        lines = [
            'partial json {"broken',  # simulates mid-line from tail seek
            json.dumps({"sessionId": "good", "inputTokens": 100, "outputTokens": 50}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_session_tail(f)
        assert result["session_id"] == "good"
        assert result["input_tokens"] == 100

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_bytes(b"")
        assert parse_session_tail(f) == {}

    def test_tool_use_extracts_last_action(self, tmp_path):
        f = tmp_path / "tools.jsonl"
        lines = [
            json.dumps({"sessionId": "s1", "toolName": "Read", "type": "tool_use"}),
            json.dumps({"toolName": "Edit", "type": "tool_use"}),
            json.dumps({"inputTokens": 50, "outputTokens": 25}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_session_tail(f)
        assert result["last_action"] == "Edit"

    def test_missing_fields_tolerated(self, tmp_path):
        f = tmp_path / "minimal.jsonl"
        # Lines with no recognized fields
        lines = [
            json.dumps({"type": "queue-operation", "operation": "enqueue"}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_session_tail(f)
        # Should return empty since no interesting fields found
        assert result == {}

    def test_all_invalid_json(self, tmp_path):
        f = tmp_path / "broken.jsonl"
        f.write_text("not json\nalso not json\n")
        assert parse_session_tail(f) == {}


class TestCollectJsonlTokens:
    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert collect_jsonl_tokens("/no/such/project") is None

    def test_returns_data_when_file_exists(self, tmp_path, monkeypatch):
        project_path = "/Users/test/proj"
        encoded = "-Users-test-proj"
        proj_dir = tmp_path / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True)

        session_file = proj_dir / "session.jsonl"
        lines = [
            json.dumps({"sessionId": "s1", "inputTokens": 500, "outputTokens": 200, "costUSD": 0.03}),
        ]
        session_file.write_text("\n".join(lines) + "\n")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = collect_jsonl_tokens(project_path)
        assert result is not None
        assert result["cost_usd"] == 0.03
        assert result["input_tokens"] == 500

    def test_never_raises(self, tmp_path, monkeypatch):
        # Force an exception inside find_session_jsonl
        def boom(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr("app.session_jsonl.find_session_jsonl", boom)
        assert collect_jsonl_tokens("/any/path") is None
