"""The call spool: buffered locally, uploaded on an interval."""

import json

import pytest

from simantic import auth, telemetry


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SIMANTIC_HOME", str(tmp_path / ".simantic"))
    monkeypatch.delenv("SIMANTIC_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    auth.save("smtc_" + "a" * 32, "dev@example.com")
    return tmp_path


class Uploads(list):
    """Captured payloads. Set `offline` to make the next upload fail."""

    offline = False


@pytest.fixture
def uploads(monkeypatch):
    sent = Uploads()

    def fake_report(event, **fields):
        if sent.offline:
            return False
        sent.append({"event": event, **fields})
        return True

    monkeypatch.setattr(telemetry, "report", fake_report)
    return sent


# --- recording ---


def test_calls_are_buffered_not_sent(uploads):
    """A round trip inside a simulation would put the network on its path."""
    telemetry.record("mcp.gdb_break")
    assert uploads == []
    assert telemetry.spool_path().exists()


def test_repeated_calls_become_counts(uploads):
    for _ in range(3):
        telemetry.record("mcp.gdb_step")
    telemetry.record("sdk.run_firmware")
    telemetry.flush(force=True)
    assert uploads[0]["calls"] == {"mcp.gdb_step": 3, "sdk.run_firmware": 1}


def test_opting_out_records_nothing(monkeypatch, uploads):
    monkeypatch.setenv("SIMANTIC_TELEMETRY", "0")
    telemetry.record("mcp.gdb_break")
    assert not telemetry.spool_path().exists()


# --- upload cadence ---


def test_the_first_flush_is_due(uploads):
    telemetry.record("cli.status")
    assert telemetry.flush() is True


def test_a_recent_upload_is_not_due_again(uploads):
    telemetry.record("cli.status")
    telemetry.flush()
    telemetry.record("cli.status")
    assert telemetry.flush() is False
    assert len(uploads) == 1


def test_the_interval_governs_the_next_upload(uploads):
    telemetry.record("cli.status")
    telemetry.flush()
    telemetry.record("cli.install")
    # An interval of zero means "any elapsed time is enough".
    assert telemetry.flush(interval=0) is True
    assert uploads[-1]["calls"] == {"cli.install": 1}


def test_an_empty_spool_uploads_nothing(uploads):
    assert telemetry.flush(force=True) is False
    assert uploads == []


# --- durability ---


def test_the_spool_is_cleared_once_uploaded(uploads):
    telemetry.record("cli.status")
    telemetry.flush(force=True)
    telemetry.flush(force=True)
    assert len(uploads) == 1  # nothing is counted twice


def test_a_failed_upload_keeps_the_calls(uploads):
    """An offline week should report once, not not at all."""
    telemetry.record("mcp.gdb_step")
    uploads.offline = True
    assert telemetry.flush(force=True) is False

    uploads.offline = False
    telemetry.record("mcp.gdb_break")
    telemetry.flush(force=True)
    assert uploads[0]["calls"] == {"mcp.gdb_step": 1, "mcp.gdb_break": 1}


def test_calls_during_an_upload_are_not_lost(uploads, monkeypatch):
    """The spool is renamed before it is read, so a concurrent record lands
    in a fresh file rather than in one about to be deleted."""
    telemetry.record("mcp.gdb_step")

    def report_and_race(event, **fields):
        telemetry.record("mcp.gdb_continue")  # arrives mid-upload
        uploads.append({"event": event, **fields})
        return True

    monkeypatch.setattr(telemetry, "report", report_and_race)
    telemetry.flush(force=True)
    assert uploads[0]["calls"] == {"mcp.gdb_step": 1}

    monkeypatch.setattr(telemetry, "report", lambda e, **f: uploads.append({**f}) or True)
    telemetry.flush(force=True)
    assert uploads[-1]["calls"] == {"mcp.gdb_continue": 1}


def test_a_corrupt_line_does_not_lose_the_rest(uploads):
    telemetry.record("cli.status")
    with open(telemetry.spool_path(), "a") as handle:
        handle.write("not json\n")
    telemetry.record("cli.status")
    telemetry.flush(force=True)
    assert uploads[0]["calls"] == {"cli.status": 2}


def test_only_the_call_name_is_recorded(uploads):
    """Arguments are the caller's data; the name is ours."""
    telemetry.record("mcp.simulate")
    line = json.loads(telemetry.spool_path().read_text().splitlines()[0])
    assert line == {"call": "mcp.simulate"}
