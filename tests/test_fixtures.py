"""test.yaml manifest handling and UART verdicts. No sim binary required."""

import pytest

from simantic import (
    ModelLibraryUnavailable,
    ServerNotConfigured,
    SimError,
    SimRun,
    UnsupportedManifest,
    load_manifest,
    run_firmware,
)
from simantic.fixtures import MCU_LIB_ENV, mcu_lib_root

SINGLE = """\
mcu: AM6442_R5F
elf: sk_am64.elf
timeout: 15
expect:
  - "RESULT: PASS"
expect_absent:
  - "RESULT: FAIL"
"""


def write(tmp_path, text, name="test.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_parses_a_single_machine_manifest(tmp_path):
    m = load_manifest(write(tmp_path, SINGLE))
    assert m.mcu == "AM6442_R5F"
    assert m.timeout == 15
    assert m.expect == ["RESULT: PASS"]
    assert m.elf_path == tmp_path / "sk_am64.elf"
    assert m.overlay_path is None


def test_timeout_defaults_when_absent(tmp_path):
    m = load_manifest(write(tmp_path, "mcu: X\nelf: a.elf\n"))
    assert m.timeout == 15
    assert m.expect == []


def test_overlay_path_resolves_beside_the_manifest(tmp_path):
    m = load_manifest(write(tmp_path, SINGLE + "overlay: extra.repl-frag\n"))
    assert m.overlay_path == tmp_path / "extra.repl-frag"


def test_multi_machine_manifest_is_refused_with_a_reason(tmp_path):
    text = "machines:\n  a: {mcu: X, elf: a.elf}\ntimeout: 5\n"
    with pytest.raises(UnsupportedManifest, match="scenario"):
        load_manifest(write(tmp_path, text))


def test_manifest_without_an_mcu_is_refused(tmp_path):
    with pytest.raises(UnsupportedManifest, match="no mcu"):
        load_manifest(write(tmp_path, "elf: a.elf\n"))


def test_unset_model_library_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(MCU_LIB_ENV, raising=False)
    with pytest.raises(ModelLibraryUnavailable, match=MCU_LIB_ENV):
        mcu_lib_root()


def test_model_library_without_the_script_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(MCU_LIB_ENV, str(tmp_path))
    with pytest.raises(ModelLibraryUnavailable, match="parse_replx"):
        mcu_lib_root()


def test_run_passes_when_every_expectation_holds():
    run = SimRun(output="RESULT: PASS\n", exit_code=0, missing=[], forbidden=[])
    assert run.passed


def test_missing_expectation_fails_and_is_reported():
    run = SimRun(
        output="booting\n", exit_code=0, missing=["RESULT: PASS"], forbidden=[]
    )
    assert not run.passed
    report = run.failure_report()
    assert "expected but not found: 'RESULT: PASS'" in report
    assert "booting" in report


def test_forbidden_text_fails_and_is_reported():
    run = SimRun(
        output="RESULT: FAIL\n", exit_code=0, missing=[], forbidden=["RESULT: FAIL"]
    )
    assert not run.passed
    assert "present but forbidden" in run.failure_report()


def test_nonzero_exit_fails_even_with_expected_text():
    run = SimRun(output="RESULT: PASS\n", exit_code=1, missing=[], forbidden=[])
    assert not run.passed
    assert "sim exited 1" in run.failure_report()


def test_empty_transcript_is_labelled():
    run = SimRun(output="", exit_code=0, missing=["x"], forbidden=[])
    assert "(no UART output)" in run.failure_report()


def test_run_requires_exactly_one_platform_source():
    with pytest.raises(ValueError, match="exactly one"):
        run_firmware("a.elf")
    with pytest.raises(ValueError, match="exactly one"):
        run_firmware("a.elf", repl="b.repl", mcu="X")


def test_run_refuses_an_unknown_backend():
    with pytest.raises(ValueError, match="backend must be"):
        run_firmware("a.elf", repl="b.repl", backend="qemu")


def test_missing_server_is_distinguished_from_a_firmware_failure(tmp_path):
    """Detected from what sim reports, since not every installation needs one."""
    fake = tmp_path / "sim"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'error: no sim-server configured (pass --server or set SIM_SERVER_URL)' >&2\n"
        "exit 2\n"
    )
    fake.chmod(0o755)
    with pytest.raises(ServerNotConfigured, match="SIM_SERVER_URL"):
        run_firmware("a.elf", repl="b.repl", binary=fake)


def test_other_failures_stay_plain_sim_errors(tmp_path):
    fake = tmp_path / "sim"
    fake.write_text("#!/bin/sh\necho 'error: cannot read elf' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(SimError, match="cannot read elf") as exc:
        run_firmware("a.elf", repl="b.repl", binary=fake)
    assert not isinstance(exc.value, ServerNotConfigured)
