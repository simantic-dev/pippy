"""Peer self-tests: no firmware, no simulator.

Each peer under ../peers is loaded by path and driven through the same callback
sequence the engine uses, with a mock `ctx`. Good peers are checked against
known-answer vectors computed here from the specs (not by calling the peer's
own helpers); malicious peers are checked to really produce their fault.

    pytest examples/tests/test_peer_mocks.py
"""
import importlib.util
import math
import struct
import zlib
from pathlib import Path

PEERS = Path(__file__).resolve().parent.parent / "peers"


class Ctx:
    mac = "02:00:00:00:00:99"

    def __init__(self):
        self.now_us = 0
        self.sent = []       # (args, kwargs) of ctx.send
        self.wire = []       # ctx.send_wire payloads
        self.order = []      # "send"/"wire", in call order
        self.timers = []     # ("periodic"|"oneshot"|"cancel", us)

    def send(self, *a, **k):
        self.sent.append((a, k))
        self.order.append("send")

    def send_wire(self, data):
        self.wire.append(bytes(data))
        self.order.append("wire")

    def schedule_periodic(self, us):
        self.timers.append(("periodic", us))

    def schedule_oneshot(self, us):
        self.timers.append(("oneshot", us))

    def cancel_timer(self):
        self.timers.append(("cancel", 0))

    def log(self, *a):
        pass

    info = warning = error = debug = noisy = log


def load(rel, cls):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, PEERS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = Ctx()
    return getattr(mod, cls)(ctx), ctx, mod


def payloads(ctx):
    return [bytes(a[0]) for a, _ in ctx.sent]


# ---- independent reference implementations (from the specs) ----------------

def crc8_dvbs2(data):
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0xD5) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    crc = 0
    for b in data:
        crc = table[crc ^ b]
    return crc


def fletcher8(data):
    a = b = 0
    for x in data:
        a = (a + x) % 256
        b = (b + a) % 256
    return bytes((a, b))


def ones_complement_sum(data):
    data = data + b"\0" * (len(data) % 2)
    s = 0
    for i in range(0, len(data), 2):
        s += data[i] << 8 | data[i + 1]
    while s > 0xFFFF:
        s = (s >> 16) + (s & 0xFFFF)
    return s


def udp_ok(ip_frame):
    """True if the IPv4 header and UDP checksums in an Ethernet frame verify."""
    ip, udp = ip_frame[14:34], ip_frame[34:]
    pseudo = ip[12:20] + bytes((0, 17)) + udp[4:6]
    return ones_complement_sum(ip) == 0xFFFF and ones_complement_sum(pseudo + udp) == 0xFFFF


# ---- UART ------------------------------------------------------------------

def test_gps_acks_valset_and_streams_pvt_at_programmed_rate():
    p, ctx, _ = load("uart/good_gps.py", "Peer")

    def valset(key, val):
        body = bytes((0x06, 0x8A)) + (4 + 4 + 4).to_bytes(2, "little") + bytes((0, 1, 0, 0)) + struct.pack("<II", key, val)
        return b"\xb5\x62" + body + fletcher8(body)

    for key, val in ((0x30210001, 100), (0x20910007, 1)):   # CFG-RATE-MEAS = 100 ms, MSGOUT NAV-PVT UART1 = 1
        for b in valset(key, val):
            p.on_byte(b)
    ack_body = bytes((0x05, 0x01, 0x02, 0x00, 0x06, 0x8A))
    assert payloads(ctx)[0] == b"\xb5\x62" + ack_body + fletcher8(ack_body)
    assert ("periodic", 100_000) in ctx.timers
    p.on_timer()
    pvt = payloads(ctx)[-1]
    assert pvt[:6] == b"\xb5\x62\x01\x07\x5c\x00" and pvt[-2:] == fletcher8(pvt[2:-2])
    assert pvt[6 + 20] == 3 and pvt[6 + 23] == 11     # fixType, numSV
    assert struct.unpack_from("<i", pvt, 6 + 24)[0] == round(-80.5449e7)


def test_gps_drops_a_bad_checksum_without_acking():
    p, ctx, _ = load("uart/good_gps.py", "Peer")
    for b in b"\xb5\x62\x06\x8a\x00\x00\x00\x00":
        p.on_byte(b)
    assert ctx.sent == []


def test_crsf_rc_frame_known_answer():
    assert crc8_dvbs2(b"123456789") == 0xBC            # CRC-8/DVB-S2 check value
    p, ctx, mod = load("uart/good_crsf.py", "Peer")
    assert ctx.timers == [("periodic", 4000)]
    p.on_timer()
    f = payloads(ctx)[0]
    assert len(f) == 26 and f[:3] == b"\xc8\x18\x16" and f[25] == crc8_dvbs2(f[2:25])
    bits = int.from_bytes(f[3:25], "little")
    assert [(bits >> (11 * i)) & 0x7FF for i in range(16)] == mod.CHANNELS


def test_malicious_uart_faults_in_order():
    p, ctx, mod = load("uart/malicious_uart.py", "Peer")
    for _ in range(6):
        p.on_timer()
    ok, badcrc, trunc, junk, badlen = payloads(ctx)       # tick 5 is silence: nothing sent
    assert len(payloads(ctx)) == 5
    assert ok[25] == crc8_dvbs2(ok[2:25])
    assert badcrc[25] != crc8_dvbs2(badcrc[2:25]) and len(badcrc) == 26
    assert len(trunc) == 10 and trunc[0] == 0xC8 and trunc == ok[:10]
    assert junk == bytes([0xFF, 0x00] * 4)
    assert badlen[1] == 0xFF and len(badlen) == 26


def test_malicious_uart_late_then_never_reply_and_link_loss():
    p, ctx, _ = load("uart/malicious_uart.py", "Peer")
    p.on_byte(0x3F)                                       # request 1: answered late
    for _ in range(75):
        p.on_timer()
    assert b"!" not in payloads(ctx)
    p.on_timer()
    assert b"!" in payloads(ctx)
    n = len(payloads(ctx))
    p.on_byte(0x3F)                                       # request 2: never answered
    for _ in range(100):
        p.on_timer()
    assert payloads(ctx)[n:].count(b"!") == 0
    for _ in range(600):
        p.on_timer()
    assert ("cancel", 0) in ctx.timers                    # the link goes silent for good


# ---- I2C -------------------------------------------------------------------

def read_reg(s, reg, n):
    s.on_write(bytes([reg]))
    return s.on_read(n)


def test_ina228_known_answers_follow_the_firmware_config():
    s, ctx, _ = load("i2c/good_ina228.py", "Slave")
    assert read_reg(s, 0x01, 2) == b"\xfb\x68"            # ADC_CONFIG reset: SLYS021A 7.6.1.2
    s.on_write(bytes([0x02, 0x10, 0x00]))                 # SHUNT_CAL = 0x1000
    s.on_finish()
    s.on_write(bytes([0x01, 0xFB, 0x68]))                 # (re)start continuous conversions
    s.on_finish()
    assert read_reg(s, 0x05, 3) == b"\x00\x00\x00"        # nothing converted yet
    ctx.now_us = 3156 - 1                                 # 3 x 1052 us, AVG = 1: one us short
    assert read_reg(s, 0x0B, 2) == b"\x00\x01"
    ctx.now_us = 3156
    assert read_reg(s, 0x0B, 2) == b"\x00\x03"            # CNVRF set
    assert read_reg(s, 0x0B, 2) == b"\x00\x01"            # cleared by the read
    assert read_reg(s, 0x05, 3) == (round(16.8 / 195.3125e-6) << 4).to_bytes(3, "big")   # 86016 << 4 = 0x150000
    lsb = 0x1000 / (13107.2e6 * 0.0005)                   # Eq. 2 = 625 uA
    assert read_reg(s, 0x07, 3) == (round(12.0 / lsb) << 4).to_bytes(3, "big")           # 19200 << 4 = 0x04B000
    assert read_reg(s, 0x08, 3) == round(16.8 * 12.0 / (3.2 * lsb)).to_bytes(3, "big")   # Eq. 5


def test_icp20100_pressure_pair_known_answer():
    s, ctx, _ = load("i2c/good_icp20100.py", "Slave")
    assert read_reg(s, 0xD3, 1) == b"\xb2"
    s.on_write(bytes([0xC0, 0x08]))                       # MEAS_MODE = 1, mode 0 = 25 Hz
    s.on_finish()
    ctx.now_us = 40_000
    p, t = 99614, -12098                                  # (100.4-70)*2^17/40, (22-25)*2^18/65, truncated
    exp = bytes((p & 0xFF, p >> 8 & 0xFF, p >> 16, t & 0xFF, t >> 8 & 0xFF, (t >> 16) & 0x0F))
    assert read_reg(s, 0xFA, 6) == exp


def tf_frame(dist, strength, head=b"\x59\x59"):
    body = head + struct.pack("<HHH", dist, strength, (25 + 256) * 8)
    return body, sum(body) % 256


def test_tf02_malicious_phases():
    s, ctx, _ = load("i2c/malicious_tf02.py", "Slave")
    trig = bytes((0x5A, 0x05, 0x00, 0x01, 0x60))
    out = []
    for _ in range(5):
        s.on_write(trig)
        out.append(s.on_read(9))
    body, ck = tf_frame(1250, 3000)
    assert out[0] == body + bytes([ck])                              # control
    assert out[1] == body + bytes([(ck + 1) % 256])                  # bad checksum
    assert out[2][:2] == b"\x59\x58"                                 # bad header
    assert out[3][2:6] == b"\xff\xff\x00\x00"                        # out of range, no strength
    assert out[4] == out[0]                                          # stuck: same value again
    s.on_write(bytes((0x5A, 0x04, 0x01, 0x5F)))
    assert s.on_read(7)[2] == 0xEE                                   # wrong ID byte (a good part says 0x01)


# ---- SPI -------------------------------------------------------------------

def spi(s, tx):
    out = [s.on_transmit(b) for b in tx]
    s.on_finish()
    return bytes(out)


def test_icm42688_good_who_am_i_and_first_fifo_packet():
    s, ctx, _ = load("spi/good_icm42688.py", "Slave")
    assert spi(s, [0xF5, 0]) == b"\x00\x47"
    spi(s, [0x16, 0x40])                                  # FIFO_CONFIG: stream mode
    spi(s, [0x4E, 0x0F])                                  # PWR_MGMT0: gyro + accel low noise
    ctx.now_us = 30_000                                   # start-up done: first sample exists
    assert spi(s, [0xAE, 0, 0]) == b"\x00\x00\x01"        # FIFO_COUNT = 1 record
    pkt = spi(s, [0xB0] + [0] * 16)[1:]
    th = math.radians(10.0)
    ax, az = int(2048 * math.sin(th)), int(2048 * math.cos(th))   # +-16 g: 2048 LSB/g
    assert pkt == bytes([0x68]) + struct.pack(">6h", ax, 0, az, 0, 0, 0) + b"\x00" + struct.pack(">H", 30_000)


def test_malicious_icm42688_faults():
    s, ctx, _ = load("spi/malicious_icm42688.py", "Slave")
    assert spi(s, [0xF5, 0])[1] == 0x42                   # wrong WHO_AM_I
    assert spi(s, [0xAE, 0, 0])[1:] == b"\x00\x04"        # claims 4 records
    pkt = spi(s, [0xB0] + [0] * 16)[1:]
    assert pkt[0] == 0x80 and pkt[1:] == bytes(15)        # ...but every packet is "FIFO empty"
    g1, g2 = spi(s, [0xA5] + [0] * 6)[1:], spi(s, [0xA5] + [0] * 6)[1:]
    assert g1 == g2 == b"\x12\x34" * 3                    # gyro stuck
    a1, a2 = spi(s, [0x9F] + [0] * 6)[1:], spi(s, [0x9F] + [0] * 6)[1:]
    assert struct.unpack(">3h", a1)[2] == 2048 and struct.unpack(">3h", a2)[2] == 2048 ^ 0x4000   # bit 14 flipped


# ---- CAN -------------------------------------------------------------------

def test_dronecan_allocation_then_nodestatus():
    n, ctx, _ = load("can/good_dronecan.py", "Node")
    assert ctx.timers == [("oneshot", 3_000_000)]
    n.on_timer()                                          # stage 1 request: anonymous
    (a, k), = ctx.sent
    uid = bytes(range(0x50, 0x60))
    assert a[0] == (24 << 24 | 0x1234 << 10 | 1 << 8) and k["extended"]
    assert a[1] == bytes([42 << 1 | 1]) + uid[:6] + b"\xc0"
    alloc = 24 << 24 | 1 << 8 | 1                         # allocator = node 1
    n.on_frame({"id": alloc, "data": bytes([0]) + uid[:6] + b"\xc0", "extended": True})
    assert ctx.sent[-1][0][1] == bytes([42 << 1]) + uid[6:12] + b"\xc1"
    n.on_frame({"id": alloc, "data": bytes([0]) + uid[:12] + b"\xc1", "extended": True})
    assert ctx.sent[-1][0][1] == bytes([42 << 1]) + uid[12:] + b"\xc2"
    n.on_frame({"id": alloc, "data": bytes([42 << 1]) + uid + b"\xc3", "extended": True})   # grant
    assert ("periodic", 1_000_000) in ctx.timers
    n.on_timer()
    (cid, data), k = ctx.sent[-1]
    assert cid == 0x1801552A                              # prio 24, NodeStatus 341, node 42
    assert data == struct.pack("<IBH", 1, 0, 0) + b"\xc3"


def test_malicious_can_echo_rotation():
    n, ctx, _ = load("can/malicious_echo.py", "Node")
    for _ in range(5):
        n.on_frame({"id": 0x123, "data": b"\x01\x02\x03\x04", "extended": False})
    got = [(a[0], a[1]) for a, _ in ctx.sent]
    assert got[0] == (0x123, b"\x01\x02\x03\xfb")         # last byte flipped
    assert got[1] == (0x124, b"\x01\x02\x03\x04")         # wrong ID
    assert got[2] == (0x123, b"\x01\x02\x03")             # DLC short
    assert got[3] == (0x123, b"\x01\x02\x03\x04\xa5")     # DLC long
    assert got[4] == (0x123, b"\x01\x02\x03\x04")         # control


# ---- Ethernet --------------------------------------------------------------

DUT_MAC, DUT_IP, PEER_IP = bytes.fromhex("020000000001"), bytes((192, 168, 1, 10)), bytes((192, 168, 1, 1))
PEER_MAC = bytes.fromhex("020000000099")


def test_ipv4_checksum_reference_vector():
    # classic worked example: header 4500 0073 0000 4000 4011 ---- c0a8 0001 c0a8 00c7, checksum b861
    hdr = bytes.fromhex("450000730000400040110000c0a80001c0a800c7")
    assert (~ones_complement_sum(hdr)) & 0xFFFF == 0xB861


def test_arp_reply_known_answer():
    p, ctx, _ = load("eth/good_arp_udp.py", "Peer")
    req = (b"\xff" * 6 + DUT_MAC + b"\x08\x06" + struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
           + DUT_MAC + DUT_IP + bytes(6) + PEER_IP)
    p.on_frame({"bytes": req})
    exp = (DUT_MAC + PEER_MAC + b"\x08\x06" + struct.pack(">HHBBH", 1, 0x0800, 6, 4, 2)
           + PEER_MAC + PEER_IP + DUT_MAC + DUT_IP)
    assert payloads(ctx)[0] == exp + bytes(60 - len(exp))


def udp_request(payload, bad_ip=False):
    udp = struct.pack(">HHHH", 5000, 7, 8 + len(payload), 0) + payload
    pseudo = DUT_IP + PEER_IP + bytes((0, 17)) + udp[4:6]
    udp = udp[:6] + struct.pack(">H", (~ones_complement_sum(pseudo + udp)) & 0xFFFF) + udp[8:]
    ip = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0) + DUT_IP + PEER_IP
    ck = (~ones_complement_sum(ip)) & 0xFFFF
    ip = ip[:10] + struct.pack(">H", ck ^ bad_ip) + ip[12:]
    f = PEER_MAC + DUT_MAC + b"\x08\x00" + ip + udp
    return f + bytes(max(0, 60 - len(f)))


def test_udp_echo_has_valid_checksums_and_swapped_endpoints():
    p, ctx, _ = load("eth/good_arp_udp.py", "Peer")
    p.on_frame({"bytes": udp_request(b"hello")})
    r = payloads(ctx)[0]
    assert r[:6] == DUT_MAC and r[6:12] == PEER_MAC and r[12:14] == b"\x08\x00"
    assert r[26:30] == PEER_IP and r[30:34] == DUT_IP
    assert struct.unpack(">HH", r[34:38]) == (7, 5000)
    assert r[42:47] == b"hello" and udp_ok(r[:14 + 20 + 8 + 5])


def test_udp_echo_ignores_a_bad_ip_checksum():
    p, ctx, _ = load("eth/good_arp_udp.py", "Peer")
    p.on_frame({"bytes": udp_request(b"hello", bad_ip=0x1111)})
    assert ctx.sent == []


def test_malicious_eth_sequence():
    p, ctx, _ = load("eth/malicious_frames.py", "Peer")
    for _ in range(7):
        p.on_timer()
    assert ctx.order == ["send", "wire", "send", "send", "send", "send", "send"]
    s = payloads(ctx)
    good1, runt, foreign, bad_ip, bad_udp, good7 = s
    wire, = ctx.wire
    assert wire[-4:] != zlib.crc32(wire[:-4]).to_bytes(4, "little")        # FCS is wrong...
    assert wire[-4:] == bytes(b ^ 0xFF for b in zlib.crc32(wire[:-4]).to_bytes(4, "little"))   # ...inverted CRC-32
    assert len(runt) == 30
    assert foreign[:6] == bytes.fromhex("020000000077") and udp_ok(foreign[:14 + 20 + 8 + 17])
    assert not udp_ok(bad_ip[:14 + 20 + 8 + 17]) and ones_complement_sum(bad_ip[14:34]) != 0xFFFF
    assert ones_complement_sum(bad_udp[14:34]) == 0xFFFF and not udp_ok(bad_udp[:14 + 20 + 8 + 17])
    for f, marker in ((good1, 1), (good7, 7)):
        assert f[:6] == DUT_MAC and f[42] == marker and udp_ok(f[:14 + 20 + 8 + 17])


def test_scenarios_parse_and_reference_existing_scripts():
    import yaml
    for sc in PEERS.rglob("scenario.yaml"):
        doc = yaml.safe_load(sc.read_text())
        for m in doc["machines"].values():
            for part in m["parts"]:
                assert (sc.parent / part["script"]).is_file(), (sc, part)
