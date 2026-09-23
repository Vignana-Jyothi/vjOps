"""Unit tests for the two executor.py handlers touched by the Verification
Agent work: `health_check` (new) and `set_memory_limit` (now captures the
pre-change value so a failed verification can propose a rollback to it).

The rest of executor.py has no unit tests in this repo yet — that's a
pre-existing gap, not something introduced here — so this file is scoped to
just the new/changed surface rather than retrofitting coverage for
everything else in one pass.
"""

import json
import urllib.error
from unittest.mock import patch

import pytest

from viljaops_agent import executor


# --------------------------------------------------------------------------- #
# health_check
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int, body: bytes = b'{"status":"ok"}'):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_health_check_ok_on_200():
    with patch("urllib.request.urlopen", return_value=_FakeResponse(200)):
        result = executor.health_check({"port": 3005, "path": "/health"})
    assert result["ok"] is True
    assert result["detail"]["status_code"] == 200
    assert result["detail"]["url"] == "http://127.0.0.1:3005/health"


def test_health_check_adds_leading_slash_to_path():
    with patch("urllib.request.urlopen", return_value=_FakeResponse(200)) as mock_open:
        executor.health_check({"port": 3005, "path": "health"})  # no leading slash
    called_url = mock_open.call_args[0][0]
    assert called_url == "http://127.0.0.1:3005/health"


def test_health_check_fails_on_http_error():
    err = urllib.error.HTTPError(url="http://127.0.0.1:3005/health", code=500, msg="err", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        result = executor.health_check({"port": 3005, "path": "/health"})
    assert result["ok"] is False
    assert result["detail"]["status_code"] == 500
    assert "500" in result["error"]


def test_health_check_fails_gracefully_on_connection_refused():
    """The app not answering at all must resolve to ok=False, not crash the
    agent — this is exactly the case the Verification Agent needs to treat
    as decisive evidence."""
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
        result = executor.health_check({"port": 3005, "path": "/health"})
    assert result["ok"] is False
    assert result["detail"]["status_code"] is None
    assert "refused" in result["error"]


def test_health_check_requires_a_port():
    with pytest.raises(executor.ExecutionError):
        executor.health_check({"path": "/health"})


def test_health_check_via_execute_dispatch():
    """Goes through the same execute() entrypoint the agent's main loop
    uses, not just the handler directly — confirms it's actually wired into
    HANDLERS."""
    with patch("urllib.request.urlopen", return_value=_FakeResponse(200)):
        result = executor.execute({"kind": "health_check", "payload": {"port": 3005, "path": "/health"}})
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# set_memory_limit — previous-value capture for rollback
# --------------------------------------------------------------------------- #
def _inspect_output(memory_bytes: int) -> str:
    return json.dumps([{"HostConfig": {"Memory": memory_bytes}}])


def test_set_memory_limit_captures_previous_value():
    calls = []

    def fake_docker(*args, timeout=30):
        calls.append(args)
        if args[0] == "inspect":
            return 0, _inspect_output(512 * 1024 * 1024), ""
        if args[0] == "update":
            return 0, "", ""
        raise AssertionError(f"unexpected docker call: {args}")

    with patch.object(executor, "_docker", side_effect=fake_docker):
        result = executor.set_memory_limit({"container": "team-app", "memory_mb": 1024})

    assert result["ok"] is True
    assert result["detail"]["memory_mb"] == 1024
    assert result["detail"]["previous_memory_mb"] == 512
    assert calls[0][0] == "inspect"  # inspected before changing anything
    assert calls[1] == ("update", "--memory", "1024m", "--memory-swap", "1024m", "team-app")


def test_set_memory_limit_survives_inspect_failure():
    """If `docker inspect` fails or returns something unexpected, the update
    should still proceed — the previous value is a nice-to-have for
    rollback, not a precondition for the fix itself."""

    def fake_docker(*args, timeout=30):
        if args[0] == "inspect":
            return 1, "", "no such container"
        if args[0] == "update":
            return 0, "", ""
        raise AssertionError(f"unexpected docker call: {args}")

    with patch.object(executor, "_docker", side_effect=fake_docker):
        result = executor.set_memory_limit({"container": "team-app", "memory_mb": 1024})

    assert result["ok"] is True
    assert result["detail"]["previous_memory_mb"] is None


def test_set_memory_limit_still_rejects_out_of_range_values():
    with pytest.raises(executor.ExecutionError):
        executor.set_memory_limit({"container": "team-app", "memory_mb": 999999})


def test_set_memory_limit_refuses_protected_containers():
    with pytest.raises(executor.ExecutionError):
        executor.set_memory_limit({"container": "viljaops-control-plane", "memory_mb": 1024})
