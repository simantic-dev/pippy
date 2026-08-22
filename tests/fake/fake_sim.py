"""A stand-in for `sim --control-stdio` that speaks the wire protocol.

Prints what the firmware would: a boot banner, then echoes sent UART text
back as "echo: <text>" once time advances. Enough to exercise Sim without an
engine.
"""
import json
import sys

t = 0.0
uart = []
inbox = []
argv = sys.argv[1:]
assert "--control-stdio" in argv, argv
machines = ["m1"] if "--scenario" not in argv else ["a", "b"]
uart.append({"t": 0.0001, "machine": machines[0], "label": "UART0", "text": "boot\r\n"})
uart.append({"t": 0.0002, "machine": machines[0], "label": "UART0", "text": "> "})


def advance(dt):
    global t
    t += dt
    while inbox:
        uart.append({"t": t, "machine": machines[0], "label": "UART0", "text": "echo: " + inbox.pop(0)})


print(json.dumps({"ready": True, "machines": machines, "pid": 1}), flush=True)
print("Renode log line that is not JSON", flush=True)
for line in sys.stdin:
    req = json.loads(line)
    op = req["op"]
    rep = {"id": req["id"], "ok": True}
    if op == "run_for":
        advance(req["seconds"]); rep["t"] = t
    elif op == "time":
        rep["t"] = t
    elif op == "send":
        inbox.append(req["text"] if "text" in req else bytes.fromhex(req["hex"]).decode())
    elif op == "expect":
        import re
        advance(0.01)
        text = "".join(r["text"] for r in uart)
        m = re.search(req["pattern"], text)
        rep.update(matched=bool(m), text=m.group(0) if m else text, t=t)
    elif op in ("read_uart", "read_frames", "read_logs"):
        src = uart if op == "read_uart" else []
        c = req.get("cursor", 0)
        rep.update(records=src[c:], next=len(src), truncated=False)
    elif op == "read_memory":
        rep.update(address=0x20000000, hex="efbeadde")
    elif op == "symbol":
        rep["address"] = 0x08000494
    elif op == "threads":
        rep["value"] = {"rtos": "Zephyr", "threads": []}
    elif op == "stop":
        print(json.dumps(rep), flush=True); break
    else:
        rep = {"id": req["id"], "ok": False, "error": f"unknown op '{op}'"}
    print(json.dumps(rep), flush=True)
