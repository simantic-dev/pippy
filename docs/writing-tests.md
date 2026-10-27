# Writing tests against scripted peers

You have firmware and a scenario with scripted peers (a GPS on a UART, an IMU on
SPI, a barometer on I2C). This guide shows how to turn that into a pytest suite
that asserts on what the peers saw and what the firmware did with it. The worked
sample is `tests/test_peers.py` (with `tests/test_peer_mocks.py`) in the
[simantic-dev/examples](https://github.com/simantic-dev/examples) repo, which
drives the peers in its `peers/` directory. It is a
trimmed copy of a suite written for a flight controller: five tests here, eight
in the original, one run of 12 virtual seconds (about 30 s of wall time there).

Peer and `scenario.yaml` reference (plain paths, on the Simantic site):
/docs/reference/peers, /docs/reference/scenario-yaml. The session methods used
below are in [session-api.md](session-api.md).

## Shape of a suite

1. **One simulation per module.** Booting and running firmware is the expensive
   part; asserting is free. Run once in a `scope="module"` fixture, collect
   everything you will want (decoded output, bus frames), and let each test
   look at a different slice.
2. **Assert on the wire, not on the firmware's own claims.** Count frames per
   peer (the bundle's bus frames, each record has `label` = the part name, `t`, `data`,
   `miso`, ...) and decode the firmware's real output (here MAVLink read with
   the recorded UART bytes, which is byte-accurate; `read_uart()` is not).
3. **Expected values live in the peer's constants.** The GPS peer reports a 3D
   fix at a fixed position, the IMU peer tilts the board by `PITCH_TRIM_DEG`.
   The test asserts the firmware reports what the peer put on the wire, and its
   comment names the constant. Change a peer, update one number.
4. **A known firmware bug is a strict `xfail`.** It documents the bug with a
   reason, keeps the suite green, and turns red the day someone fixes the bug
   (XPASS strict), prompting you to delete the marker.

## The shared run

```python
@pytest.fixture(scope="module")
def run():
    scenario = yaml.safe_load((PEERS / "scenario.yaml").read_text())
    scenario["machines"]["dut"]["elf"] = str(Path(elf).resolve())
    return simantic.run_scenario(scenario, timeout=RUN_SECONDS, base=PEERS)  # virtual seconds
```

- `run_scenario` takes the same dict shape as `sim run --scenario`'s YAML and
  runs the `sim` binary, peers included; `base=` is the base for relative
  `elf:` and `script:` paths, and `args=` passes any other `sim run` flag. It
  returns the recorded bundle as `run.output`, `run.uart_records`,
  `run.interrupts`, `run.frames`, `run.stdout` (trace output) and `run.stderr` (peer output).
- `Sim` is single-machine and has no peers; use it for stepping, not for this.
- The sample skips when `FIRMWARE` is unset: the firmware is yours to supply.

## The assertions

```python
def test_gps_fix_reaches_mavlink(run):
    msgs, _ = run
    assert last(msgs, GPS_RAW_INT)[28] >= 3        # gps_peer reports a 3D fix

def test_baro_is_read_over_i2c(run):
    _, bus = run
    assert bus["baro"] > 1000                      # frames the baro peer saw

@pytest.mark.xfail(strict=True, reason="known firmware bug: ...")
def test_baro_reaches_scaled_pressure(run):
    ...
```

Frames are timestamped in virtual time, so a rate is a subtraction between
consecutive frames and an ordering is a comparison (see the README's "Test what
went over the wire").

## Running it

```
pip install simantic pytest
FIRMWARE=/path/to/firmware.elf pytest tests   # from a simantic-dev/examples checkout
```

If a test fails, read the peer's own log first: peers report with `ctx.info` /
`ctx.warning`; run the `sim` binary with `-v` to see the simulator's log.

## Things this sample does not do

- It does not check MAVLink CRCs (the original does, with a small helper).
- Peers are plant-free constants: a fixed fix, a stationary board. Closing a
  loop against a physics model is a different job.
