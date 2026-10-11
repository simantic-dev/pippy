# Examples

| File | What it shows |
| --- | --- |
| `step_and_peek.py` | Plain script, no pytest: boot a board, `expect()` a line, read memory, step time. `python step_and_peek.py fw.elf STM32F401RE` |
| `parallel_sweep.py` | Run the same firmware N times in parallel and collect when each reaches a line. |
| `tests/test_peers.py` | pytest suite over the three-peer `peers/scenario.yaml`: one shared run per module, strict `xfail`. Needs your firmware; see `docs/writing-tests.md`. |
| `tests/test_peer_mocks.py` | Self-test of every peer below with a mock `ctx`: no firmware, no simulator. `pytest examples/tests/test_peer_mocks.py` |

## Scripted peers: one good and one malicious per bus

A good peer follows the part's spec; a malicious one injects faults so the
firmware's validation, timeouts and recovery are exercised. Each file's header
says what it models, which datasheet or RFC it follows and, for malicious
ones, the exact faults and what a robust firmware should do. Each bus directory
has a `scenario.yaml` that runs the good and the malicious peer as two machines
against your own `firmware.elf`.

| Bus | Good peer | Malicious peer |
| --- | --- | --- |
| UART | `peers/uart/good_gps.py`: u-blox UBX GPS, ACKs CFG-VALSET, streams NAV-PVT at the rate the firmware programs. `peers/uart/good_crsf.py`: CRSF RC frames at 250 Hz, crc8 DVB-S2, 420 kbaud. | `peers/uart/malicious_uart.py`: CRSF-style link with bad CRC, truncated frames, garbage, bad length, a late then missing reply, then permanent silence. |
| I2C | `peers/i2c/good_icp20100.py`: TDK ICP-20100 barometer, register file and FIFO. `peers/i2c/good_ina228.py`: TI INA228 power monitor, conversion time from the firmware's ADC_CONFIG and SHUNT_CAL. | `peers/i2c/malicious_tf02.py`: TF02-Pro-style LiDAR with a wrong ID, bad checksum, bad header, out-of-range and stuck readings. |
| SPI | `peers/spi/good_icm42688.py`: TDK ICM-42688-P IMU, registers, FIFO, ODR from the firmware's config. | `peers/spi/malicious_icm42688.py`: wrong WHO_AM_I, FIFO that reports a count but returns empty packets, stuck gyro, bit-flipped accel. |
| CAN | `peers/can/good_dronecan.py`: DroneCAN anonymous node-ID allocation, then 1 Hz NodeStatus. | `peers/can/malicious_echo.py`: echoes each frame with a flipped last byte, wrong ID, short DLC, long DLC, then once clean. |
| Ethernet | `peers/eth/good_arp_udp.py`: answers ARP, echoes UDP on port 7 with correct IPv4 and UDP checksums. | `peers/eth/malicious_frames.py`: bad FCS, runt, foreign MAC, bad IPv4 checksum, bad UDP checksum, between good frames. |

`peers/scenario.yaml` wires the GPS, IMU and barometer peers to one machine
(used by `tests/test_peers.py`).

Limits to know about:

- I2C: scripted slaves cannot yet NACK a data byte or stretch SCL mid-transfer
 , so the I2C malicious peer does not inject either.
  The CLI guide describes an `on_address` hook that returns False
  to NACK the address; these examples do not use or test it.
- Ethernet: the bad-FCS frame uses `ctx.send_wire`, present in the pyrite
  engine's peer only; the malicious peer skips that frame (with a warning) where
  it is missing. The addresses in `malicious_frames.py` (`DUT_MAC`, `DUT_IP`)
  are constants to set to your firmware's.
- A peer's `ctx` has a single timer, so peers that need several delays count
  ticks of one periodic timer.

No firmware is shipped. The peers hold their expected values as constants at
the top of each file; the tests read those same numbers.

Peer and `scenario.yaml` reference: /docs/reference/peers, /docs/reference/scenario-yaml
