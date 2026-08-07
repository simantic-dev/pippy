"""Typed view of the `analog-cli test --format json` report.

Mirrors schemas/test-report.schema.json ("analog-cli.test-report/1"). Parsing
is tolerant of additive fields — the schema allows those within a revision —
and strict about the shape discriminator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA = "analog-cli.test-report/1"

#: Statuses that do not fail a run. `not_implemented` marks a test kind the
#: installed CLI does not support; `skipped` one inapplicable to the project.
#: Neither is an error: a testplan may name more than the CLI can run today.
PASSING_STATUSES = frozenset({"pass", "skipped", "not_implemented"})


class ReportError(ValueError):
    """The report was absent, unparseable, or of an unknown shape."""


@dataclass(frozen=True)
class Expect:
    min: float | None = None
    max: float | None = None
    eq: float | None = None
    tol: float | None = None

    @property
    def informational(self) -> bool:
        """An empty expect block never fails."""
        return self.min is None and self.max is None and self.eq is None


@dataclass(frozen=True)
class Measurement:
    name: str
    expect: Expect
    passed: bool
    signal: str | None = None
    measured: float | None = None
    #: Signed distance to the nearest bound; >= 0 passes, magnitude is headroom.
    margin: float | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Measurement:
        expect = data.get("expect") or {}
        return cls(
            name=data["name"],
            expect=Expect(**{k: v for k, v in expect.items() if k in Expect.__annotations__}),
            passed=data["pass"],
            signal=data.get("signal"),
            measured=data.get("measured"),
            margin=data.get("margin"),
        )

    def describe(self) -> str:
        """One-line human summary, used in pytest failure output."""
        where = f"{self.signal} " if self.signal else ""
        got = "not evaluated" if self.measured is None else f"{self.measured:g}"
        bounds = []
        if self.expect.min is not None:
            bounds.append(f"min {self.expect.min:g}")
        if self.expect.max is not None:
            bounds.append(f"max {self.expect.max:g}")
        if self.expect.eq is not None:
            tol = f" +/- {self.expect.tol:g}" if self.expect.tol is not None else ""
            bounds.append(f"eq {self.expect.eq:g}{tol}")
        limit = ", ".join(bounds) if bounds else "informational"
        margin = "" if self.margin is None else f", margin {self.margin:g}"
        return f"{self.name}: {where}measured {got} (expected {limit}{margin})"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    description: str
    sheet: str | None = None

    def describe(self) -> str:
        where = f" [{self.sheet}]" if self.sheet else ""
        return f"{self.severity} {self.kind}{where}: {self.description}"


@dataclass(frozen=True)
class Test:
    # Not a pytest test class, despite the name.
    __test__ = False

    name: str
    kind: str
    status: str
    detail: str | None = None
    measurements: list[Measurement] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Test:
        return cls(
            name=data["name"],
            kind=data["kind"],
            status=data["status"],
            detail=data.get("detail"),
            measurements=[Measurement.from_json(m) for m in data.get("measurements", [])],
            findings=[
                Finding(
                    kind=f["kind"],
                    severity=f["severity"],
                    description=f["description"],
                    sheet=f.get("sheet"),
                )
                for f in data.get("findings", [])
            ],
        )

    @property
    def passed(self) -> bool:
        return self.status in PASSING_STATUSES

    def failure_report(self) -> str:
        """Multi-line explanation of why this test did not pass."""
        lines = [f"{self.name} ({self.kind}): {self.status}"]
        if self.detail:
            lines.append(f"  {self.detail}")
        for m in self.measurements:
            if not m.passed:
                lines.append(f"  FAIL {m.describe()}")
        for f in self.findings:
            lines.append(f"  {f.describe()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Summary:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    not_implemented: int


@dataclass(frozen=True)
class TestReport:
    # Not a pytest test class, despite the name.
    __test__ = False

    cli_version: str
    project: str
    plan: str
    started_unix: int
    duration_seconds: float
    summary: Summary
    tests: list[Test]
    kicad_cli: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TestReport:
        schema = data.get("schema")
        if schema != SCHEMA:
            raise ReportError(f"expected schema {SCHEMA!r}, got {schema!r}")
        s = data["summary"]
        return cls(
            cli_version=data["cli_version"],
            project=data["project"],
            plan=data["plan"],
            started_unix=data["started_unix"],
            duration_seconds=data["duration_seconds"],
            summary=Summary(
                total=s["total"],
                passed=s["passed"],
                failed=s["failed"],
                errors=s["errors"],
                skipped=s["skipped"],
                not_implemented=s["not_implemented"],
            ),
            tests=[Test.from_json(t) for t in data["tests"]],
            kicad_cli=data.get("kicad_cli"),
        )

    @property
    def passed(self) -> bool:
        """True when no test failed or errored (skips do not fail a run)."""
        return self.summary.failed == 0 and self.summary.errors == 0

    def test(self, name: str) -> Test:
        for t in self.tests:
            if t.name == name:
                return t
        raise KeyError(f"no test named {name!r} in report for {self.project}")
