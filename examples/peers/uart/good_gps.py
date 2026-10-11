# u-blox M9/M10-class UBX GPS as a scripted UART peer.
# Protocol: u-blox UBX framing (sync B5 62, class, id, length LE16, payload,
# 8-bit Fletcher checksum over class..payload). Known-good vector:
#   CFG-VALSET 06 8A 09 00 00 01 00 00 07 00 91 20 01 -> ckA=53 ckB=48
# Behaviour: ACK every CFG-VALSET, read the measurement rate and nav ratio the
# firmware programs (CFG-RATE-MEAS/NAV), then stream a static 3D-fix NAV-PVT at
# exactly that period. Only the modern CFG-VALSET path is modelled.
import struct

SYNC = b"\xb5\x62"
CLASS_NAV, CLASS_ACK, CLASS_CFG = 0x01, 0x05, 0x06
ID_NAV_PVT, ID_ACK_ACK, ID_CFG_VALSET = 0x07, 0x01, 0x8A
KEY_RATE_MEAS, KEY_RATE_NAV, KEY_MSGOUT_NAV_PVT_UART1 = 0x30210001, 0x30210002, 0x20910007
DEFAULT_RATE_MEAS_MS, DEFAULT_RATE_NAV = 1000, 1  # u-blox power-on defaults

LAT_DEG, LON_DEG, ALT_M, NUM_SV = 43.4723, -80.5449, 334.0, 11  # static fix


def checksum(body: bytes) -> bytes:
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def frame(cls: int, mid: int, payload: bytes = b"") -> bytes:
    body = bytes((cls, mid)) + struct.pack("<H", len(payload)) + payload
    return SYNC + body + checksum(body)


def nav_pvt(itow_ms: int) -> bytes:
    p = bytearray(92)
    struct.pack_into("<I", p, 0, itow_ms & 0xFFFFFFFF)
    struct.pack_into("<HBBBBB", p, 4, 2026, 9, 15, 12, 0, 0)
    p[11], p[20], p[21], p[23] = 0x07, 3, 0x01, NUM_SV  # valid, fixType 3D, gnssFixOK, numSV
    struct.pack_into("<iiii", p, 24, round(LON_DEG * 1e7), round(LAT_DEG * 1e7),
                     round(ALT_M * 1000), round(ALT_M * 1000))  # lon lat height hMSL
    return frame(CLASS_NAV, ID_NAV_PVT, bytes(p))


class Peer:
    def __init__(self, ctx):
        self.ctx, self.rx = ctx, bytearray()
        self.itow_ms = 0
        self.rate_meas = self.rate_nav = None
        self.pvt_on = False
        self.period_us = None

    def on_byte(self, b):
        self.rx.append(b)
        while True:
            while len(self.rx) >= 2 and self.rx[:2] != SYNC:
                del self.rx[0]
            if len(self.rx) < 8:
                return
            total = 8 + (self.rx[4] | self.rx[5] << 8)
            if len(self.rx) < total:
                return
            raw = bytes(self.rx[:total])
            del self.rx[:total]
            self._frame(raw)

    def _frame(self, raw):
        body = raw[2:-2]
        if checksum(body) != raw[-2:]:
            self.ctx.warning(f"bad checksum, dropping {raw.hex()}")
            return
        if body[:2] == bytes((CLASS_CFG, ID_CFG_VALSET)):
            # payload: version layers reserved(2) key(4) value(<=4); body[4:] is the payload
            key = struct.unpack_from("<I", body, 8)[0]
            val = int.from_bytes(body[12:], "little")
            self.ctx.send(frame(CLASS_ACK, ID_ACK_ACK, bytes((CLASS_CFG, ID_CFG_VALSET))))
            self.ctx.info(f"CFG-VALSET key=0x{key:08x} value={val} -> ACK-ACK")
            if key == KEY_MSGOUT_NAV_PVT_UART1:
                self.pvt_on = val == 1
            elif key == KEY_RATE_MEAS:
                self.rate_meas = val
            elif key == KEY_RATE_NAV:
                self.rate_nav = val
            self._maybe_stream()

    def _maybe_stream(self):
        # The firmware enables NAV-PVT before it programs the rate (observed
        # on a real driver), so re-arm the timer whenever the period changes.
        meas = self.rate_meas or DEFAULT_RATE_MEAS_MS
        nav = self.rate_nav or DEFAULT_RATE_NAV
        period = meas * nav * 1000
        if not self.pvt_on or period == self.period_us:
            return
        self.period_us = period
        self.ctx.info(f"NAV-PVT every {meas * nav} ms (as programmed by the firmware)")
        self.ctx.schedule_periodic(period)

    def on_timer(self):
        self.itow_ms += self.rate_meas or DEFAULT_RATE_MEAS_MS
        self.ctx.send(nav_pvt(self.itow_ms))
