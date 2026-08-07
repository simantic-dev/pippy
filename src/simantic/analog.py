"""Driving `analog-cli` from Python.

The CLI is the source of truth; this module is a typed subprocess wrapper
around its JSON contract, not a reimplementation of anything it does.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

from ._locate import analog_cli
from .report import ReportError, TestReport

#: Exit codes that still produce a report: everything ran, verdicts inside.
#: 0 = all passed, 1 = at least one test failed or errored.
_REPORT_EXITS = frozenset({0, 1})

_EXIT_MEANINGS = {
    2: "bad project",
    3: "kicad-cli not found",
    4: "the runner itself failed",
    6: "testplan invalid or unreadable",
}


class AnalogCliError(RuntimeError):
    """analog-cli exited with a code that carries no report."""

    def __init__(self, code: int, stderr: str) -> None:
        meaning = _EXIT_MEANINGS.get(code, "unknown failure")
        super().__init__(f"analog-cli exited {code} ({meaning})\n{stderr.strip()}")
        self.code = code
        self.stderr = stderr


def run_tests(
    project: str | os.PathLike[str],
    *,
    plan: str | os.PathLike[str] | None = None,
    only: Sequence[str] | None = None,
    binary: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
) -> TestReport:
    """Run a project's testplan and return the parsed report.

    `project` is a directory or .kicad_pro. Without `plan`, analog-cli uses
    the project's <name>.sim.toml, falling back to its built-in static checks.
    A failing test is a normal outcome and comes back in the report; only
    conditions that prevent a run at all raise AnalogCliError.
    """
    cmd = [str(analog_cli(binary)), "test", "-p", str(project), "--format", "json"]
    if plan is not None:
        cmd += ["--plan", str(plan)]
    for name in only or ():
        cmd += ["--only", name]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode not in _REPORT_EXITS:
        raise AnalogCliError(proc.returncode, proc.stderr)

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ReportError(
            f"analog-cli exited {proc.returncode} but did not emit JSON: {exc}"
        ) from exc
    return TestReport.from_json(data)


def plan_path(project: str | os.PathLike[str]) -> Path | None:
    """The <name>.sim.toml a project directory would use, if it exists."""
    path = Path(project)
    directory = path.parent if path.suffix == ".kicad_pro" else path
    candidates = sorted(directory.glob("*.sim.toml"))
    return candidates[0] if candidates else None


def plan_test_names(plan: str | os.PathLike[str]) -> list[str]:
    """Names of the [[test]] tables in a .sim.toml testplan, in file order.

    Read directly so a test runner can enumerate cases without invoking the
    CLI once per collection. Unnamed tables are skipped: --only matches by
    name, so a nameless test is not individually addressable.
    """
    with open(plan, "rb") as fh:
        data = tomllib.load(fh)
    return [t["name"] for t in data.get("test", []) if "name" in t]
