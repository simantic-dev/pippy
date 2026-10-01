"""test.yaml manifest handling and UART verdicts. No sim binary required."""

import pytest
from pathlib import Path

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


def test_multi_machine_manifest_becomes_a_scenario(tmp_path):
    (tmp_path / "peer.repl-frag").write_text('p: UART.ScriptedUartPeer @ sysbus 0xA0000000\n    file: "{TEST_DIR}/peer.py"\n')
    text = (
        "machines:\n"
        "  dut: {mcu: X, elf: a.elf, overlay: peer.repl-frag}\n"
        "  b: {mcu: Y, elf: b.elf}\n"
        "media:\n  - {type: uart, connect: [dut.usart1, dut.p]}\n"
        "timeout: 5\nquantum: 0.0001\n"
        "expect: ['[dut] RESULT: PASS']\nexpect_frames: ['SPI Rx cs=0']\n"
    )
    m = load_manifest(write(tmp_path, text))
    assert not m.single and m.timeout == 5 and m.expect_frames == ["SPI Rx cs=0"]
    sc = m.scenario(tmp_path)
    assert set(sc["machines"]) == {"dut", "b"}
    assert sc["machines"]["dut"]["elf"] == str((tmp_path / "a.elf").resolve())
    assert sc["media"] == [{"type": "uart", "connect": ["dut.usart1", "dut.p"]}]
    assert sc["quantum"] == 0.0001
    # The overlay is copied with {TEST_DIR} expanded, so peer file: paths resolve anywhere.
    frag = Path(sc["machines"]["dut"]["overlay"]).read_text()
    assert "{TEST_DIR}" not in frag and str(tmp_path.resolve()) in frag


def test_parts_scripts_resolve_beside_the_manifest(tmp_path):
    text = (
        "mcu: X\nelf: a.elf\n"
        "parts:\n"
        "  - {name: baro, type: i2c-device, bus: i2c1, address: 0x63, script: baro.py}\n"
        "  - {name: card, type: sd-card, bus: sdmmc1, size: 16MiB, image: card.img}\n"
    )
    parts = load_manifest(write(tmp_path, text)).scenario(tmp_path)["machines"]["dut"]["parts"]
    assert parts[0]["script"] == str(tmp_path.resolve() / "baro.py") and parts[0]["address"] == 0x63
    assert parts[1]["image"] == str(tmp_path.resolve() / "card.img") and parts[1]["size"] == "16MiB"


def test_wall_budget_defaults_and_override(tmp_path):
    from simantic.fixtures import WALL_WIRED_S, WALL_WIRELESS_S
    assert load_manifest(write(tmp_path, SINGLE)).wall_budget == WALL_WIRED_S
    assert load_manifest(write(tmp_path, SINGLE + "wireless: true\n")).wall_budget == WALL_WIRELESS_S
    assert load_manifest(write(tmp_path, SINGLE + "wall: 7\n")).wall_budget == 7


def test_run_within_stops_at_the_wall_budget():
    from simantic.pytest_plugin import _run_within

    class SlowSim:
        time = 0.0
        def run_for(self, s):
            import time
            time.sleep(0.05)          # 50 ms of host time per chunk
            self.time += s
    fast = SlowSim()
    assert _run_within(fast, 1.0, wall_s=10, step=0.5) is True and abs(fast.time - 1.0) < 1e-9
    slow = SlowSim()
    assert _run_within(slow, 100.0, wall_s=0.12, step=0.5) is False and slow.time < 100.0


def test_single_machine_manifest_is_a_one_machine_scenario(tmp_path):
    m = load_manifest(write(tmp_path, SINGLE))
    assert m.single
    assert list(m.scenario(tmp_path)["machines"]) == ["dut"]


def test_render_uart_and_frames_match_the_cli_line_shapes():
    from simantic.pytest_plugin import render_frames, render_uart
    uart = [{"machine": "dut", "label": "UART2", "text": "RESULT: \x01PASS\r\n"},
            {"machine": "b", "label": "UART0", "text": "hi\n"}]
    assert render_uart(uart, multi=False).splitlines()[0] == "RESULT: .PASS"
    assert "[dut] RESULT: .PASS" in render_uart(uart, multi=True)
    frames = [{"t": 1.0885, "machine": "dut", "label": "icp", "protocol": "I2c", "direction": "Tx",
               "summary": "addr=0x63 read len=2 data=FF 00"}]
    assert render_frames(frames, multi=False) == "[1.088500s] (icp) I2C Tx addr=0x63 read len=2 data=FF 00"
    assert render_frames(frames, multi=True).startswith("[dut] [1.088500s]")


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
