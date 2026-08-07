"""Testplan enumeration and binary lookup. No analog-cli binary required."""

from pathlib import Path

import pytest

from simantic import BinaryNotFound, analog_cli, plan_path, plan_test_names
from simantic._locate import ENV_VAR

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "divider"
PLAN = FIXTURE / "divider.sim.toml"


def test_enumerates_named_tests_in_file_order():
    assert plan_test_names(PLAN) == ["schematic-erc", "netlist-sane", "rails-op"]


def test_unnamed_tests_are_skipped():
    """--only matches by name, so a nameless table is not addressable."""
    assert "drc" not in plan_test_names(PLAN)


def test_plan_path_finds_the_projects_testplan():
    assert plan_path(FIXTURE) == PLAN


def test_plan_path_accepts_a_kicad_pro():
    assert plan_path(FIXTURE / "divider.kicad_pro") == PLAN


def test_plan_path_is_none_without_a_testplan(tmp_path):
    assert plan_path(tmp_path) is None


def test_env_var_overrides_lookup(tmp_path, monkeypatch):
    fake = tmp_path / "analog-cli"
    fake.touch()
    monkeypatch.setenv(ENV_VAR, str(fake))
    assert analog_cli() == fake


def test_missing_env_target_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
    with pytest.raises(BinaryNotFound, match="does not exist"):
        analog_cli()


def test_explicit_path_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
    explicit = tmp_path / "chosen"
    explicit.touch()
    assert analog_cli(explicit) == explicit
