"""Report parsing, checked against the shapes schemas/test-report.schema.json
allows. No analog-cli binary required.
"""

import pytest

from simantic import ReportError, TestReport

BASE = {
    "schema": "analog-cli.test-report/1",
    "cli_version": "0.1.0",
    "project": "rc_divider",
    "plan": "rc_divider.sim.toml",
    "started_unix": 1770000000,
    "duration_seconds": 1.5,
    "summary": {
        "total": 3,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
        "not_implemented": 0,
    },
    "tests": [
        {
            "name": "rails-op",
            "kind": "op",
            "status": "pass",
            "measurements": [
                {
                    "name": "out-dc",
                    "signal": "V(OUT)",
                    "measured": 1.597,
                    "expect": {"eq": 1.597, "tol": 0.02},
                    "margin": 0.02,
                    "pass": True,
                }
            ],
        },
        {
            "name": "startup-settling",
            "kind": "tran",
            "status": "fail",
            "detail": "1 of 1 measurement failed",
            "measurements": [
                {
                    "name": "settle-time",
                    "signal": "V(OUT)",
                    "measured": 0.0082,
                    "expect": {"max": 0.006},
                    "margin": -0.0022,
                    "pass": False,
                }
            ],
        },
        {
            "name": "board-drc",
            "kind": "drc",
            "status": "skipped",
            "detail": "no .kicad_pcb",
        },
    ],
}


def test_parses_summary_and_tests():
    report = TestReport.from_json(BASE)
    assert report.project == "rc_divider"
    assert report.summary.total == 3
    assert len(report.tests) == 3


def test_failed_run_is_not_passed():
    assert not TestReport.from_json(BASE).passed


def test_skips_alone_do_not_fail_a_run():
    data = BASE | {
        "summary": BASE["summary"] | {"failed": 0, "passed": 2},
        "tests": [t for t in BASE["tests"] if t["status"] != "fail"],
    }
    report = TestReport.from_json(data)
    assert report.passed
    assert report.test("board-drc").passed


def test_not_implemented_never_fails():
    data = BASE | {
        "summary": BASE["summary"] | {"failed": 0, "not_implemented": 1, "passed": 1},
        "tests": [{"name": "noise-floor", "kind": "noise", "status": "not_implemented"}],
    }
    assert TestReport.from_json(data).passed


def test_measurement_describe_carries_the_numbers():
    report = TestReport.from_json(BASE)
    text = report.test("startup-settling").measurements[0].describe()
    assert "V(OUT)" in text
    assert "0.0082" in text
    assert "max 0.006" in text


def test_failure_report_explains_the_failing_measurement():
    text = TestReport.from_json(BASE).test("startup-settling").failure_report()
    assert "startup-settling (tran): fail" in text
    assert "FAIL settle-time" in text


def test_findings_are_parsed():
    data = BASE | {
        "tests": [
            {
                "name": "schematic-erc",
                "kind": "erc",
                "status": "fail",
                "findings": [
                    {
                        "kind": "pin_not_connected",
                        "severity": "error",
                        "description": "U1 pin 3 unconnected",
                        "sheet": "/power",
                    }
                ],
            }
        ]
    }
    finding = TestReport.from_json(data).test("schematic-erc").findings[0]
    assert finding.sheet == "/power"
    assert "pin_not_connected" in finding.describe()


def test_additive_fields_are_tolerated():
    """The schema allows additive fields within a revision."""
    data = BASE | {"future_field": 1}
    data["tests"] = [BASE["tests"][0] | {"future_field": 2}]
    assert TestReport.from_json(data).test("rails-op").passed


def test_unknown_schema_revision_is_refused():
    with pytest.raises(ReportError, match="analog-cli.test-report/1"):
        TestReport.from_json(BASE | {"schema": "analog-cli.test-report/2"})


def test_informational_measurement_has_no_bounds():
    data = BASE | {
        "tests": [
            {
                "name": "counts",
                "kind": "erc",
                "status": "pass",
                "measurements": [
                    {"name": "warnings", "measured": 2.0, "expect": {}, "pass": True}
                ],
            }
        ]
    }
    m = TestReport.from_json(data).test("counts").measurements[0]
    assert m.expect.informational
    assert "informational" in m.describe()


def test_missing_test_name_raises():
    with pytest.raises(KeyError):
        TestReport.from_json(BASE).test("nope")
