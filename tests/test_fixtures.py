"""test.yaml manifest handling and UART verdicts. No sim binary required."""

import os

import pytest
from pathlib import Path

from simantic import (
    SimError,
    SimRun,
    UnsupportedManifest,
    load_manifest,
    run_firmware,
)

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
    m = load_manifest(write(tmp_path, SINGLE + "overlay: extra.board\n"))
    assert m.overlay_path == tmp_path / "extra.board"


def test_multi_machine_manifest_becomes_a_scenario(tmp_path):
    (tmp_path / "peer.board").write_text('p: UART.ScriptedUartPeer @ sysbus 0xA0000000\n    file: "{TEST_DIR}/peer.py"\n')
    text = (
        "machines:\n"
        "  dut: {mcu: X, elf: a.elf, overlay: peer.board}\n"
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


def test_single_machine_manifest_is_a_one_machine_scenario(tmp_path):
    m = load_manifest(write(tmp_path, SINGLE))
    assert m.single
    assert list(m.scenario(tmp_path)["machines"]) == ["dut"]


def test_render_uart_matches_the_cli_line_shape():
    from simantic.pytest_plugin import render_uart
    uart = [{"machine": "dut", "label": "UART2", "text": "RESULT: \x01PASS\r\n"},
            {"machine": "b", "label": "UART0", "text": "hi\n"}]
    assert render_uart(uart, multi=False).splitlines()[0] == "RESULT: .PASS"
    assert "[dut] RESULT: .PASS" in render_uart(uart, multi=True)


def test_manifest_without_an_mcu_is_refused(tmp_path):
    with pytest.raises(UnsupportedManifest, match="no mcu"):
        load_manifest(write(tmp_path, "elf: a.elf\n"))


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


def test_a_failed_start_is_a_sim_error(tmp_path):
    fake = tmp_path / "sim"
    fake.write_text("#!/bin/sh\necho 'error: cannot read elf' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(SimError, match="cannot read elf"):
        run_firmware("a.elf", mcu="X", binary=fake)


def test_work_dir_sparse_files_and_services_reach_the_scenario(tmp_path):
    (tmp_path / "overlay.board").write_text('card: SD.SDCard @ sdmmc2\n    imageFile: "{WORK_DIR}/sd.img"\n')
    (tmp_path / "test.yaml").write_text(
        "mcu: STM32H753ZI\nelf: fw.elf\noverlay: overlay.board\n"
        "sparse_files:\n  sd.img: 4M\n"
        "networkServices:\n  - {name: tower, host: '*', port: 0}\n")
    work = tmp_path / "work"
    work.mkdir()
    scenario = load_manifest(tmp_path / "test.yaml").scenario(work)
    assert (work / "sd.img").stat().st_size == 4 << 20
    assert f'"{work}/sd.img"' in Path(scenario["machines"]["dut"]["overlay"]).read_text()
    assert scenario["networkServices"] == [{"name": "tower", "host": "*", "port": 0}]


def test_sim_binary_flags_make_a_manifest_unsupported(tmp_path):
    (tmp_path / "test.yaml").write_text("mcu: X\nelf: fw.elf\nsim_args: ['--usb-cdc-console', 'usb_otg_fs']\n")
    with pytest.raises(UnsupportedManifest, match="--usb-cdc-console"):
        load_manifest(tmp_path / "test.yaml")
    (tmp_path / "test.yaml").write_text("mcu: X\nelf: fw.elf\nsim_args: ['--ascii']\n")
    assert load_manifest(tmp_path / "test.yaml").mcu == "X"


PARTS_MANIFEST = Path(os.environ.get("SIMANTIC_PARTS_MANIFEST", "/nonexistent"))


@pytest.mark.skipif(not (os.environ.get("SIMANTIC_SIM") and PARTS_MANIFEST.exists()),
                    reason="needs $SIMANTIC_SIM and a test.yaml with `parts:` ($SIMANTIC_PARTS_MANIFEST)")
def test_run_scenario_with_a_scripted_peer_goes_through_the_binary(tmp_path):
    from simantic import run_scenario

    m = load_manifest(PARTS_MANIFEST)
    assert m.machines["dut"]["parts"]
    run = run_scenario(m.scenario(tmp_path), m.timeout, base=m.path.parent)
    assert all(text in run.output for text in m.expect)
