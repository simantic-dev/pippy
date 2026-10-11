# Examples

| File | What it shows |
| --- | --- |
| `step_and_peek.py` | Plain script, no pytest: boot a board, `expect()` a line, read memory, step time. `python step_and_peek.py fw.elf STM32F401RE` |
| `parallel_sweep.py` | Run the same firmware N times in parallel and collect when each reaches a line. |
| `peers/gps_peer.py` | u-blox UBX GPS on a UART: ACKs CFG-VALSET, streams NAV-PVT at the rate the firmware programs. |
| `peers/icm42688_peer.py` | TDK ICM-42688-P IMU on SPI: registers, FIFO, ODR from the firmware's config. |
| `peers/icp20100_peer.py` | TDK ICP-20100 barometer on I2C: register file and FIFO. |
| `peers/scenario.yaml` | Wires the three peers to a machine. Needs your own `firmware.elf`; see the header. |
| `tests/test_peers.py` | pytest suite over those peers: one shared run per module, strict `xfail`. See `docs/writing-tests.md`. |

No firmware is shipped. The peers hold their expected values as constants at
the top of each file; the tests read those same numbers.

Peer and `scenario.yaml` reference: /docs/reference/peers, /docs/reference/scenario-yaml
