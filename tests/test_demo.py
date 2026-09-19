"""The scenario a demo writes, and the promise that it needs no account."""

from __future__ import annotations

import json

import pytest

from simantic import demo


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
