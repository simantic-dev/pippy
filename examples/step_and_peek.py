"""Plain script: boot a board, stop at a line, read memory, step time. No pytest."""
import sys
from simantic import Sim

elf, repl = sys.argv[1], sys.argv[2]
with Sim(elf=elf, repl=repl, uart="usart2") as sim:
    m = sim.expect("RESULT: (PASS|FAIL)", timeout=60)
    print(f"{m.text} at virtual t={m.virtual_seconds:.6f}s")
    print("main is at", hex(sim.symbol("main")), "first word", sim.read_memory("main").hex())
    sim.run_for(0.25)
    warnings = [l for l in sim.logs(from_start=True) if l["level"] in ("Warning", "Error")]
    print(f"{len(warnings)} simulator warnings; virtual time now {sim.time:.3f}s")
