"""Guards on the things that only break at publish time."""

import subprocess
import sys
import tomllib
from pathlib import Path

import simantic

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    """A release tags one version; two sources of truth drift silently."""
    with open(PYPROJECT, "rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert simantic.__version__ == declared


def test_pytest_plugin_entry_point_is_declared():
    """Without this, `pip install simantic` registers no collectors."""
    with open(PYPROJECT, "rb") as fh:
        entry_points = tomllib.load(fh)["project"]["entry-points"]
    assert entry_points["pytest11"]["simantic"] == "simantic.pytest_plugin"


def test_console_script_is_declared():
    """The `simantic` command; pip generates a native launcher per platform."""
    with open(PYPROJECT, "rb") as fh:
        assert tomllib.load(fh)["project"]["scripts"]["simantic"] == "simantic._cli:main"


def test_python_dash_m_reaches_the_same_cli():
    """The fallback when the launcher's directory is not on PATH."""
    proc = subprocess.run(
        [sys.executable, "-m", "simantic", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "auth" in proc.stdout and "install" in proc.stdout


def test_public_names_are_importable():
    """__all__ promises these; a rename would otherwise fail only for users."""
    for name in simantic.__all__:
        assert hasattr(simantic, name), name
