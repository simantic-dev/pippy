# TBS Crossfire / ExpressLRS receiver as a scripted UART peer (420000 8N1).
# Spec: CRSF protocol (Team BlackSheep / ExpressLRS CRSF documentation):
#   frame = address(0xC8 flight controller) | len | type | payload | crc8
#   len   = type + payload + crc bytes; crc8 = poly 0xD5 (DVB-S2), init 0,
#           over type + payload.
#   RC_CHANNELS_PACKED (type 0x16): 16 channels x 11 bits, little-endian
#   bit-packed in 22 bytes; 172 = 988 us, 992 = 1500 us, 1811 = 2012 us.
# Behaviour: streams one RC frame every 4 ms (250 Hz link rate). Sticks
# centred, throttle low, channel 5 high. The firmware never talks to it.
CHANNELS = [992, 992, 172, 992, 1811, 172, 172, 172, 992, 992, 992, 992, 992, 992, 992, 992]
PERIOD_US = 4000


def crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def rc_frame(ch):
    bits = 0
    for i, v in enumerate(ch):
        bits |= (v & 0x7FF) << (11 * i)
    body = b"\x16" + bits.to_bytes(22, "little")
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8(body)])


class Peer:
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.schedule_periodic(PERIOD_US)

    def on_byte(self, b):
        pass

    def on_timer(self):
        self.ctx.send(rc_frame(CHANNELS))
