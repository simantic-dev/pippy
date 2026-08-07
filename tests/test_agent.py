"""The MCP session: protocol handling and what counts as a failure."""

import json
import os
import stat
import sys

import pytest

from simantic import agent

# A stand-in server: one JSON-RPC response per request line, canned per method.
FAKE = """#!{python}
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, rid = req["method"], req.get("id")
    if method == "initialize":
        out = {{"result": {{"protocolVersion": "2024-11-05"}}}}
    elif method == "tools/list":
        out = {{"result": {{"tools": [{{"name": "simulate"}}, {{"name": "gdb_break"}}]}}}}
    else:
        payload = json.dumps({payload})
        out = {{"result": {{"content": [{{"type": "text", "text": payload}}],
                           "isError": True}}}}
    out.update(jsonrpc="2.0", id=rid)
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Install a fake server and return a factory for its canned payload."""

    def make(payload: str = '{"status": "completed", "error": None}'):
        path = tmp_path / "pyrite-mcp"
        path.write_text(FAKE.format(python=sys.executable, payload=payload))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("SIMANTIC_PYRITE_MCP", str(path))
        return path

    return make


def test_lists_the_tool_surface(server):
    server()
    with agent.Session() as session:
        assert session.tools() == ["simulate", "gdb_break"]


def test_a_result_arrives_as_data_not_text(server):
    """Tools return JSON inside a text block; parsing it per call is the
    boilerplate this exists to remove."""
    server('{"status": "completed", "error": None, "uart": {"text": "hi"}}')
    with agent.Session() as session:
        result = session.call("simulate", elfPath="fw.elf")
    assert result["uart"]["text"] == "hi"


def test_a_reported_error_raises(server):
    server('{"error": "no such board"}')
    with agent.Session() as session:
        with pytest.raises(agent.ToolError, match="no such board"):
            session.call("simulate", board="nope")


def test_success_is_not_mistaken_for_failure(server):
    """The server sets isError on successful calls too, so trusting the
    envelope would turn every completed run into an exception."""
    server('{"status": "completed", "error": None}')
    with agent.Session() as session:
        assert session.call("simulate")["status"] == "completed"


def test_arguments_reach_the_tool(server):
    server('{"error": None, "echo": True}')
    with agent.Session() as session:
        assert session.call("gdb_break", symbol="main")["echo"] is True


def test_a_missing_server_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SIMANTIC_PYRITE_MCP", raising=False)
    from simantic._locate import BinaryNotFound

    with pytest.raises(BinaryNotFound, match="pyrite-mcp"):
        agent.Session()


def test_the_process_is_stopped_on_exit(server):
    server()
    with agent.Session() as session:
        proc = session._proc
        assert proc.poll() is None
    assert proc.poll() is not None


def test_calling_a_closed_session_is_an_error(server):
    server()
    session = agent.Session()
    session.close()
    with pytest.raises(agent.SessionError):
        session.tools()
