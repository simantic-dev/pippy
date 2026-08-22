"""Run the same firmware N times in parallel and collect when each reaches a line.

Each Sim is its own process, so a ProcessPoolExecutor is all the scheduling
you need. Swap the parameter for anything a scenario accepts (quantum, a peer
script argument, a clock_ppm) to turn this into a real sweep.
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from simantic import Sim

ELF, REPL = sys.argv[1], sys.argv[2]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 4


def one_run(i: int) -> tuple[int, float]:
    with Sim(elf=ELF, repl=REPL, uart="usart2") as sim:
        m = sim.expect("RESULT: PASS", timeout=120)
        return i, m.virtual_seconds


if __name__ == "__main__":
    with ProcessPoolExecutor() as pool:
        for i, t in pool.map(one_run, range(N)):
            print(f"run {i}: RESULT: PASS at t={t:.6f}s")
