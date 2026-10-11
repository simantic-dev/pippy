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

- I2C: scripted slaves cannot yet NACK a data byte or stretch SCL
  mid-transfer, so the I2C malicious peer injects neither. It does NACK the
  first three address phases via `on_address` returning False, which works in
  sim 0.6.6 (the firmware under test retried and recovered).
- Ethernet: the bad-FCS frame needs `ctx.send_wire`, which sim 0.6.6 does not
  have; `malicious_frames.py` skips that frame with a warning there. The
  addresses in it (`DUT_MAC`, `DUT_IP`) are constants to set to your firmware's.
  In 0.6.6 an `eth-peer` must be a `parts:` entry; declaring
  `Network.ScriptedEthernetPeer` in an overlay fails registration.
- A peer's `ctx` has a single timer, so peers that need several delays count
  ticks of one periodic timer. The UART peer ctx has no `now_us`.

## Verified (local runs, no firmware shipped)

Firmware class: an STM32H753 flight-controller build with GPS on two UARTs,
two ICM-42688-P on SPI1, INA228, ICP-20100 and TF02-Pro on I2C, CRSF on UART4,
DroneCAN on FDCAN1 and MAVLink on USART1, 12 s virtual, `sim` 0.6.6 (C# engine).
The firmware's own reference peers were swapped for each example on its bus.

| Example | Result |
| --- | --- |
| `uart/good_gps.py` | Run. 34 `GPS_RAW_INT`, fix 3D, 11 sats, same count as the reference peer (a first version streamed at 1 Hz because the firmware enables NAV-PVT before programming the rate; fixed). |
| `uart/good_crsf.py` | Run. `RC_CHANNELS` carries 1500/1500/1000/1500/2011 us, CRC-clean MAVLink. |
| `i2c/good_ina228.py` | Run. `BATTERY_STATUS` identical to the reference peer (a first version lacked ENERGY/CHARGE; fixed). |
| `i2c/good_icp20100.py` | Run. Baro polled (about 7250 I2C transactions), run completes. |
| `spi/good_icm42688.py` | Run on both IMUs. `ATTITUDE` pitch -10.47 deg, same as reference. |
| `can/good_dronecan.py` | Run. 29 CAN frames, both directions, same as the reference. |
| `uart/malicious_uart.py` | Run. Completes. The firmware's RC output stayed at its failsafe values (1500/1500/1000/1500/1000...) for the whole run. With only truncated, garbage or bad-length faults mixed with valid frames it accepted the valid ones; with a bad-CRC frame in the stream it never did. Cause not investigated. |
| `i2c/malicious_tf02.py` | Run. 724 faulty polls served; `DISTANCE_SENSOR` stayed 1250 cm (the 65535 frame never surfaced). The firmware never sent the version query, so the wrong-ID reply was not exercised. Address NACK works and the firmware recovers. |
| `spi/malicious_icm42688.py` | Run on each IMU. On the IMU the firmware probes with WHO_AM_I the wrong ID disabled it: no `RAW_IMU`, pitch 0. The firmware never reads WHO_AM_I on the other IMU; there it read the FIFO count 4 and empty packets repeatedly without crashing. |
| `can/malicious_echo.py` | Run. Completes; 22 CAN frames, the firmware's own frames were echoed corrupted (last byte, ID, DLC) and it kept running. |
| `eth/malicious_frames.py` | Run against a prebuilt Zephyr raw-frame listener on NUCLEO-H753ZI: completes, but the listener only sees EtherType 0x88B5, so none of its IP-level faults were observable. A scratch variant (0x88B5, marker byte) showed sim 0.6.6 delivering the runt and the foreign-MAC frame to the application; bad-FCS skipped (no `send_wire`). |
| `eth/good_arp_udp.py` | Not run: no firmware with an IP stack is available. Mock test only. |
| `tests/test_peers.py` | Run with that firmware on the installed 0.6.9 engine: 4 pass, `test_baro_reaches_scaled_pressure` XPASS(strict) because this build does not have the uninitialised-struct bug the xfail documents. |

No firmware is shipped. The peers hold their expected values as constants at
the top of each file; the tests read those same numbers.

Peer and `scenario.yaml` reference: /docs/reference/peers, /docs/reference/scenario-yaml
