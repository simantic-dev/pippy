"""The cloud backend through Sim, against a fake HTTP transport. No network:
what is tested is request shaping and response parsing."""

import base64
import io
import json

import pytest

from simantic import SimError, Sim
from simantic import auth


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    auth.save("smtc_test", "dev@example.com")
    return tmp_path


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """Records every request and answers from a script keyed by "METHOD path"."""

    def __init__(self, script: dict):
        self.script = script
        self.calls = []

    def __call__(self, request, timeout=None):
        method, path = request.get_method(), request.full_url.split("/api/v1", 1)[1]
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        key = f"{method} {path.split('?')[0]}"
        if key not in self.script:
            raise AssertionError(f"unscripted request: {key}")
        return FakeResponse(self.script[key])


def _sim(tmp_path, transport, monkeypatch, **kw):
    monkeypatch.setattr("urllib.request.urlopen", transport)
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF-fake")
    return Sim(elf=elf, mcu="ESP32-C3", backend="cloud", **kw)


def test_start_uploads_elf_and_stores_session(tmp_path, monkeypatch):
    transport = FakeTransport({
        "POST /sessions": {"sessionId": "s1", "machines": ["machine"]},
        "DELETE /sessions/s1": {},
    })
    sim = _sim(tmp_path, transport, monkeypatch)
    assert sim.machines == ["machine"]
    method, path, body = transport.calls[0]
    assert (method, path) == ("POST", "/sessions")
    assert body["machines"][0]["mcu"] == "ESP32-C3"
    assert base64.b64decode(body["machines"][0]["elf"]) == b"\x7fELF-fake"
    sim.close()
    assert transport.calls[-1][:2] == ("DELETE", "/sessions/s1")


def test_run_for_and_time(tmp_path, monkeypatch):
    transport = FakeTransport({
        "POST /sessions": {"sessionId": "s1", "machines": ["machine"]},
        "POST /sessions/s1/run-for": {"virtualSeconds": 0.5},
        "GET /sessions/s1/time": {"virtualSeconds": 0.5},
        "DELETE /sessions/s1": {},
    })
    sim = _sim(tmp_path, transport, monkeypatch)
    assert sim.run_for(0.5) == 0.5
    assert sim.time == 0.5
    sim.close()


def test_read_memory_round_trips_base64(tmp_path, monkeypatch):
    transport = FakeTransport({
        "POST /sessions": {"sessionId": "s1", "machines": ["machine"]},
        "POST /sessions/s1/memory/read": {"data": base64.b64encode(b"\x01\x02\x03\x04").decode()},
        "DELETE /sessions/s1": {},
    })
    sim = _sim(tmp_path, transport, monkeypatch)
    assert sim.read_memory(0x1000, 4) == b"\x01\x02\x03\x04"
    sim.close()


def test_missing_credentials_raise_simerror(tmp_path, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF-fake")
    with pytest.raises(SimError, match="needs credentials"):
        Sim(elf=elf, mcu="ESP32-C3", backend="cloud")
