# Examples

| File | What it shows |
| --- | --- |
| `step_and_peek.py` | Plain script, no pytest: boot a board, `expect()` a line, read memory, step time. `python step_and_peek.py fw.elf STM32F401RE` |
| `parallel_sweep.py` | Run the same firmware N times in parallel and collect when each reaches a line. |

Scripted-peer examples (GPS, IMU, barometer, scenario YAML, pytest suites) live in
https://github.com/simantic-dev/examples. See `docs/writing-tests.md`.
