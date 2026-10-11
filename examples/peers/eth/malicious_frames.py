# Fault-injecting Ethernet peer: after BOOT_US it sends one frame per
# millisecond to the DUT, a fixed sequence of good frames and faults. Each
# frame is an IPv4/UDP datagram to DUT_IP:UDP_PORT whose payload's first byte
# is the frame's marker, so a test can tell which ones got through.
#   1 valid                         -> must be delivered
#   2 bad FCS (CRC-32 inverted)     -> MAC must drop it (CRC error)
#   3 runt: 30 bytes + FCS          -> MAC must drop it (under 64 on the wire)
#   4 foreign destination MAC       -> address filter must drop it
#   5 bad IPv4 header checksum      -> IP stack must drop it
#   6 bad UDP checksum              -> UDP layer must drop it
#   7 valid                         -> must be delivered (the stack recovered)
# Expected: markers 1 and 7 reach the application, 2-6 do not, and the
# firmware does not hang, leak buffers or crash on the bad ones.
# Frame 2 uses ctx.send_wire (we supply the FCS). sim 0.6.6's ctx has no
# send_wire (observed: "'_Ctx' object has no attribute 'send_wire'"), so there
# frame 2 is skipped with a warning and the run continues; an engine whose ctx
# has send_wire (the pyrite engine's peer documents it) sends it.
# Spec: IEEE 802.3 (FCS = CRC-32, sent least-significant byte first), RFC 791,
# RFC 768.
import struct
import zlib

DUT_MAC = bytes.fromhex("020000000001")   # set to the MAC the firmware programs
OTHER_MAC = bytes.fromhex("020000000077")
DUT_IP = bytes((192, 168, 1, 10))         # set to the IP the firmware uses
PEER_IP = bytes((192, 168, 1, 1))
UDP_PORT = 9000
BOOT_US = 1_500_000


def inet_sum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f">{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return s


def datagram(marker, bad_ip=False, bad_udp=False):
    payload = bytes([marker]) + b"simantic" + bytes(8)
    udp = struct.pack(">HHHH", 40000, UDP_PORT, 8 + len(payload), 0) + payload
    ck = ((~inet_sum(PEER_IP + DUT_IP + struct.pack(">BBH", 0, 17, len(udp)) + udp)) & 0xFFFF) or 0xFFFF
    udp = udp[:6] + struct.pack(">H", ck ^ (0x1111 if bad_udp else 0)) + udp[8:]
    hdr = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), marker, 0x4000, 64, 17, 0) + PEER_IP + DUT_IP
    ip_ck = ((~inet_sum(hdr)) & 0xFFFF) ^ (0x1111 if bad_ip else 0)
    return hdr[:10] + struct.pack(">H", ip_ck) + hdr[12:] + udp


def eth(dst, src, body):
    f = dst + src + b"\x08\x00" + body
    return f + bytes(max(0, 60 - len(f)))


def fcs(data):
    return zlib.crc32(data).to_bytes(4, "little")


class Peer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.mac = bytes(int(b, 16) for b in ctx.mac.split(":"))
        self.step = 0
        ctx.schedule_oneshot(BOOT_US)

    def on_frame(self, frame):
        pass

    def on_timer(self):
        c, m = self.ctx, self.mac
        if self.step == 0:
            c.send(eth(DUT_MAC, m, datagram(1)))
        elif self.step == 1:
            f = eth(DUT_MAC, m, datagram(2))
            if hasattr(c, "send_wire"):
                c.send_wire(f + bytes(b ^ 0xFF for b in fcs(f)))
            else:
                c.warning("send_wire unavailable on this engine: bad-FCS frame skipped")
        elif self.step == 2:
            c.send((DUT_MAC + m + b"\x08\x00" + datagram(3))[:30])
        elif self.step == 3:
            c.send(eth(OTHER_MAC, m, datagram(4)))
        elif self.step == 4:
            c.send(eth(DUT_MAC, m, datagram(5, bad_ip=True)))
        elif self.step == 5:
            c.send(eth(DUT_MAC, m, datagram(6, bad_udp=True)))
        elif self.step == 6:
            c.send(eth(DUT_MAC, m, datagram(7)))
        else:
            return
        self.step += 1
        c.schedule_oneshot(1000)
