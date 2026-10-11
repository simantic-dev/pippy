# Hostile CAN echo peer. Echoes every frame the firmware sends, but corrupts
# it in a rotating way so the firmware's validation of replies is exercised.
# A well-behaved peer never does this; a scripted one does it deterministically.
#   echo 0 (mod 5)  last data byte flipped (^0xFF)  -> payload compare must fail
#   echo 1          identifier + 1                  -> reply must be rejected as
#                                                      from the wrong ID
#   echo 2          DLC shortened by one byte       -> length check must fail
#   echo 3          one extra payload byte (DLC+1)  -> length check must fail
#                                                      (classic CAN: capped at 8)
#   echo 4          echoed unchanged                -> the control: accepted
# Frames with an empty payload are echoed unchanged. A robust firmware checks
# ID, DLC and payload of every reply and treats a mismatch as a bus error,
# not as data.
class Node:
    def __init__(self, ctx):
        self.ctx = ctx
        self.n = 0

    def on_frame(self, frame):
        cid, data, ext = frame["id"], bytearray(frame["data"]), bool(frame.get("extended"))
        mode = self.n % 5
        self.n += 1
        if data:
            if mode == 0:
                data[-1] ^= 0xFF
            elif mode == 2:
                del data[-1]
            elif mode == 3 and len(data) < 8:
                data.append(0xA5)
        if mode == 1:
            cid += 1
        self.ctx.info(f"echo #{self.n} mode {mode}: id=0x{cid:x} len={len(data)}")
        self.ctx.send(cid, bytes(data), extended=ext)
