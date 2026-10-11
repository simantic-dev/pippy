# Fault-injecting UART peer: a CRSF-style link that misbehaves on a schedule.
# Models a flaky receiver or a noisy wire, so firmware frame validation and
# receive timeouts get exercised. Framing is the CRSF one (see good_crsf.py):
# 0xC8 | len | type | payload | crc8 (poly 0xD5).
#
# Fault schedule, one frame per 4 ms tick (cycle repeats every 6 ticks):
#   tick 0  valid frame                     -> must be accepted
#   tick 1  valid frame, crc byte inverted  -> must be rejected (CRC mismatch)
#   tick 2  valid frame cut after 10 bytes  -> must not be accepted; the
#                                              parser must resync on the next
#                                              0xC8 and not wait for 26 bytes
#   tick 3  8 bytes of 0xFF/0x00 garbage    -> must be skipped byte-wise
#   tick 4  frame with len field 0xFF       -> must be rejected (CRSF max is 64)
#   tick 5  silence                         -> link-loss timeout/failsafe
# After SILENCE_AFTER_TICKS ticks (2 s of virtual time) the peer stops sending
# for good: the firmware should declare link loss and fail safe.
# Request/reply: a '?' byte from the firmware is answered with '!' LATE_TICKS
# ticks later (300 ms, virtual time) for odd-numbered requests and never for
# even-numbered ones, so the firmware's reply timeout and retry are exercised.
# (The peer ctx has one timer, so delays are counted in ticks of it.)
# The sequence is the same every run (no randomness).
# A robust firmware validates CRC and length before using any field, never
# blocks on a partial frame, and times out when no valid frame arrives.
PERIOD_US = 4000
SILENCE_AFTER_TICKS = 500
LATE_TICKS = 75


def crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def good_frame():
    body = b"\x16" + bytes([0x55]) * 22
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8(body)])


def faulty(tick):
    f = good_frame()
    if tick == 0:
        return f
    if tick == 1:
        return f[:-1] + bytes([f[-1] ^ 0xFF])
    if tick == 2:
        return f[:10]
    if tick == 3:
        return bytes([0xFF, 0x00] * 4)
    if tick == 4:
        return bytes([0xC8, 0xFF]) + f[2:]
    return b""


class Peer:
    def __init__(self, ctx):
        self.ctx, self.tick, self.requests, self.reply_at = ctx, 0, 0, None
        ctx.schedule_periodic(PERIOD_US)

    def on_byte(self, b):
        if b == 0x3F:
            self.requests += 1
            if self.requests % 2:
                self.reply_at = self.tick + LATE_TICKS

    def on_timer(self):
        if self.tick == self.reply_at:
            self.ctx.send(b"!")
        if self.tick >= SILENCE_AFTER_TICKS:
            self.ctx.cancel_timer()
            return
        out = faulty(self.tick % 6)
        self.tick += 1
        if out:
            self.ctx.send(out)
