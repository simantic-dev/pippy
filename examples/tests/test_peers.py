"""Peer-test sample: boot firmware against the scripted peers in ../peers and
assert on what came out, from one simulation shared by every test in the module.

    pip install simantic pytest
    FIRMWARE=/path/to/firmware.elf pytest examples/tests

You supply the firmware (it must stream MAVLink v2 on MAVLINK_UART and talk to
the three peers wired in ../peers/scenario.yaml). Expected values are the
constants at the top of each peer; change a peer and update the number here.
"""
import os
import struct
from collections import Counter
from pathlib import Path

import pytest
import yaml

from simantic import Sim

PEERS = Path(__file__).resolve().parent.parent / "peers"
MAVLINK_UART = "usart1"  # the UART your firmware streams MAVLink on
RUN_SECONDS = 12.0       # virtual seconds
HEARTBEAT, GPS_RAW_INT, SCALED_PRESSURE, ATTITUDE = 0, 24, 29, 30


def mavlink_v2(data: bytes):
    """Yield (msgid, payload) for each MAVLink v2 frame. CRC is not checked."""
    i = 0
    while i + 12 <= len(data):
        if data[i] != 0xFD:
            i += 1
            continue
        n = data[i + 1]
        if i + 12 + n > len(data):
            break
        yield int.from_bytes(data[i + 7:i + 10], "little"), data[i + 10:i + 10 + n]
        i += 12 + n


@pytest.fixture(scope="module")
def run():
    elf = os.environ.get("FIRMWARE")
    if not elf or not Path(elf).exists():
        pytest.skip("set FIRMWARE=/path/to/firmware.elf")
    scenario = yaml.safe_load((PEERS / "scenario.yaml").read_text())
    scenario["machines"]["dut"]["elf"] = str(Path(elf).resolve())
    # cwd resolves the peers' `script:` paths; parts need the default renode backend.
    with Sim(scenario=scenario, machine="dut", uart=MAVLINK_UART, cwd=PEERS) as s:
        s.run_for(RUN_SECONDS)
        msgs = list(mavlink_v2(s.read_uart_bytes(from_start=True)))
        bus = Counter(f["label"] for f in s.frames(from_start=True))
    return msgs, bus


def last(msgs, mid):
    p = [p for m, p in msgs if m == mid][-1]
    return p.ljust(64, b"\0")  # MAVLink v2 trims trailing zero bytes


def test_mavlink_stream_has_the_expected_messages(run):
    msgs, _ = run
    assert {m for m, _ in msgs} >= {HEARTBEAT, GPS_RAW_INT, SCALED_PRESSURE, ATTITUDE}


def test_gps_fix_reaches_mavlink(run):
    msgs, _ = run
    assert last(msgs, GPS_RAW_INT)[28] >= 3  # fix_type: gps_peer reports a 3D fix


def test_imu_is_polled_and_attitude_follows(run):
    msgs, bus = run
    assert bus["imu"] > 100
    pitch_deg = struct.unpack_from("<f", last(msgs, ATTITUDE), 8)[0] * 57.2958
    assert -13 < pitch_deg < -7  # icm42688_peer PITCH_TRIM_DEG = 10, firmware sign convention


def test_baro_is_read_over_i2c(run):
    _, bus = run
    assert bus["baro"] > 1000


@pytest.mark.xfail(strict=True, reason="known firmware bug: SCALED_PRESSURE is sent from an "
                   "uninitialised struct on ticks where no baro sample is ready")
def test_baro_reaches_scaled_pressure(run):
    msgs, _ = run
    assert 1000 < struct.unpack_from("<f", last(msgs, SCALED_PRESSURE), 4)[0] < 1008  # hPa
