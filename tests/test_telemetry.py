"""Usage reporting: what is sent, and every way it stays quiet."""

import json

import pytest

from simantic import auth, telemetry


@pytest.fixture(autouse=True)
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SIMANTIC_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    auth.save("smtc_" + "a" * 32, "dev@example.com")
    return tmp_path


def sent(monkeypatch):
    """Capture the request a report would make."""
    seen = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *e): return False

    def capture(request, **kwargs):
        seen.append(request)
        return Response()

    monkeypatch.setattr(telemetry.urllib.request, "urlopen", capture)
    return seen


# --- what is sent ---


def test_a_session_is_reported(monkeypatch):
    seen = sent(monkeypatch)
    assert telemetry.report("pytest-session", passed=3, failed=1) is True
    body = json.loads(seen[0].data)
    assert body["event"] == "pytest-session"
    assert body["passed"] == 3 and body["failed"] == 1
    assert body["client"] == "simantic-py"


def test_reports_go_to_the_sdk_endpoint(monkeypatch):
    """Not report-usage: that feeds run statistics, which count every row as a
    simulation and a missing exit code as a failure."""
    seen = sent(monkeypatch)
    telemetry.report("usage", calls={"mcp.gdb_step": 1})
    assert seen[0].full_url.endswith("/report-sdk-usage")


def test_the_token_authenticates_the_report(monkeypatch):
    seen = sent(monkeypatch)
    telemetry.report("pytest-session", passed=1)
    assert seen[0].get_header("Authorization") == "Bearer smtc_" + "a" * 32


def test_nothing_identifying_is_sent(monkeypatch):
    """Customer firmware paths and project names are their IP, not our metric."""
    seen = sent(monkeypatch)
    telemetry.report("pytest-session", passed=1)
    body = json.loads(seen[0].data)
    assert set(body) == {
        "event", "cli_version", "client", "python", "os", "arch", "passed",
    }


def test_an_oversized_report_is_dropped_not_rejected(monkeypatch):
    seen = sent(monkeypatch)
    assert telemetry.report("pytest-session", blob="x" * (16 * 1024)) is False
    assert seen == []


# --- every way it stays quiet ---


@pytest.mark.parametrize(
    "env", [{"SIMANTIC_TELEMETRY": "0"}, {"DO_NOT_TRACK": "1"}]
)
def test_opting_out_stops_it(env, monkeypatch):
    seen = sent(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert telemetry.report("pytest-session", passed=1) is False
    assert seen == []


def test_an_unauthenticated_user_reports_nothing(account, monkeypatch):
    """No account means no one to attribute a report to."""
    seen = sent(monkeypatch)
    (account / ".sim_id").unlink()
    assert telemetry.report("pytest-session", passed=1) is False
    assert seen == []


def test_a_network_failure_is_swallowed(monkeypatch):
    """Telemetry that breaks a test run is worse than no telemetry."""
    def explode(*a, **k):
        raise telemetry.urllib.error.URLError("no route to host")

    monkeypatch.setattr(telemetry.urllib.request, "urlopen", explode)
    assert telemetry.report("pytest-session", passed=1) is False


def test_describe_states_the_opt_out(monkeypatch):
    assert "SIMANTIC_TELEMETRY=0" in telemetry.describe()
    monkeypatch.setenv("SIMANTIC_TELEMETRY", "0")
    assert telemetry.describe() == "telemetry: disabled"
