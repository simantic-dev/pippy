"""The scenario a demo writes, and the promise that it needs no account."""

from __future__ import annotations

import json

import pytest

from simantic import demo, telemetry


def test_ble_pair_scenario_is_two_nodes_on_one_medium(tmp_path):
    entry = demo.DEMOS["ble-pair"]
    scenario = json.loads(demo.write_scenario(entry, tmp_path).read_text())

    assert sorted(scenario["machines"]) == ["central", "peripheral"]
    assert len(scenario["media"]) == 1
    medium = scenario["media"][0]
    assert medium["type"] == "ble"
    # Both radios on the same medium, or the two nodes never hear each other.
    assert sorted(medium["connect"]) == ["central.radio", "peripheral.radio"]


def test_ble_pair_quantum_resolves_ifs():
    """BLE's T_IFS is 150 us; a coarser quantum loses the response window."""
    assert demo.DEMOS["ble-pair"].quantum is not None
    assert demo.DEMOS["ble-pair"].quantum <= 150e-6 / 2


def test_scenario_paths_are_bare_names(tmp_path):
    """`sim` resolves repl/elf against the scenario file's own directory."""
    entry = demo.DEMOS["ble-pair"]
    scenario = json.loads(demo.write_scenario(entry, tmp_path).read_text())
    for machine in scenario["machines"].values():
        for key in ("repl", "elf"):
            assert "/" not in machine[key]
            assert machine[key] in entry.assets


def test_unknown_demo_lists_the_known_ones():
    with pytest.raises(demo.DemoError, match="available: ble-pair"):
        demo.run("nope")


def test_demo_report_needs_no_credentials_and_no_identifier(monkeypatch):
    """Anonymous by construction: no Authorization, nothing that links runs."""
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def capture(request, timeout=None):
        sent["headers"] = {k.lower(): v for k, v in request.header_items()}
        sent["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("simantic.auth.load", lambda: pytest.fail("no credentials"))
    monkeypatch.setattr("urllib.request.urlopen", capture)
    assert telemetry.report_demo("ble-pair", ok=True, seconds=4.2)

    assert "authorization" not in sent["headers"]
    payload = sent["payload"]
    assert payload["demo"] == "ble-pair" and payload["ok"] is True
    # No stable identifier of any kind, or these stop being anonymous.
    for key in ("user", "email", "token", "id", "install_id", "machine_id"):
        assert key not in payload


def test_demo_report_honours_opt_out(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("sent"))
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert not telemetry.report_demo("ble-pair", ok=True, seconds=1.0)
    monkeypatch.delenv("DO_NOT_TRACK")
    monkeypatch.setenv("SIMANTIC_TELEMETRY", "0")
    assert not telemetry.report_demo("ble-pair", ok=True, seconds=1.0)


def test_demo_report_never_breaks_the_run(monkeypatch):
    """A telemetry failure must not change what the demo returns."""

    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert telemetry.report_demo("ble-pair", ok=True, seconds=1.0) is False


def test_running_a_demo_never_loads_credentials(tmp_path, monkeypatch):
    """The whole point: a demo runs before anyone has signed up."""
    monkeypatch.setattr(demo.install, "simantic_home", lambda: tmp_path)

    def fail(*_args, **_kwargs):
        raise AssertionError("a demo must not read credentials")

    monkeypatch.setattr("simantic.auth.load", fail)
    entry = demo.DEMOS["ble-pair"]
    target = demo.demo_root() / entry.name
    target.mkdir(parents=True)
    for name in entry.assets:
        (target / name).write_bytes(b"")

    # Assets already cached, so this must not reach the network either.
    monkeypatch.setattr(demo, "_manifest", fail)
    assert demo.fetch_assets(entry) == target


def test_unauthenticated_model_error_says_what_to_do(tmp_path, monkeypatch):
    """The first wall a new user hits: actions, not a file path."""
    from simantic import _replx
    from simantic.mcu import SimError

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SIMANTIC_HOME", str(tmp_path))
    monkeypatch.delenv("SIMANTIC_MCU_LIB", raising=False)
    with pytest.raises(SimError) as caught:
        _replx.model_replx("ESP32-C3")

    message = str(caught.value)
    assert "ESP32-C3 needs an account" in message
    assert "simantic auth" in message and "simantic demo" in message
    # The path to ~/.sim_id is a detail the reader cannot act on.
    assert ".sim_id" not in message
