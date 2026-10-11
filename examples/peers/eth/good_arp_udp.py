# Minimal Ethernet host as a scripted eth-peer: answers ARP requests for its
# IP and echoes UDP datagrams sent to it, with correct checksums.
# Specs: RFC 826 (ARP), RFC 791 (IPv4 header checksum, 16-bit one's
# complement), RFC 768 (UDP, checksum over the pseudo-header; a computed 0 is
# sent as 0xFFFF), RFC 862 (echo). `frame` handed to on_frame has `bytes`
# (without FCS); ctx.send takes bytes from the destination MAC on and the
# medium appends the FCS. The peer's MAC is the eth-peer's `mac:`.
import struct

PEER_IP = bytes((192, 168, 1, 1))
UDP_PORT = 7


def inet_sum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f">{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return s


def udp_checksum(src, dst, udp):
    s = inet_sum(src + dst + struct.pack(">BBH", 0, 17, len(udp)) + udp)
    return ((~s) & 0xFFFF) or 0xFFFF


def ipv4_udp(src_ip, dst_ip, sport, dport, payload):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    udp = udp[:6] + struct.pack(">H", udp_checksum(src_ip, dst_ip, udp)) + udp[8:]
    hdr = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0) + src_ip + dst_ip
    return hdr[:10] + struct.pack(">H", (~inet_sum(hdr)) & 0xFFFF) + hdr[12:] + udp


def eth(dst, src, ethertype, body):
    f = dst + src + struct.pack(">H", ethertype) + body
    return f + bytes(max(0, 60 - len(f)))   # pad to the 60-byte minimum (without FCS)


class Peer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.mac = bytes(int(b, 16) for b in ctx.mac.split(":"))

    def on_frame(self, frame):
        f = bytes(frame["bytes"])
        if len(f) < 42:
            return
        src_mac, ethertype = f[6:12], f[12:14]
        if ethertype == b"\x08\x06" and f[20:22] == b"\x00\x01" and f[38:42] == PEER_IP:   # ARP request for us
            reply = struct.pack(">HHBBH", 1, 0x0800, 6, 4, 2) + self.mac + PEER_IP + f[22:28] + f[28:32]
            self.ctx.send(eth(src_mac, self.mac, 0x0806, reply))
        elif ethertype == b"\x08\x00" and f[14] == 0x45 and f[23] == 17 and f[30:34] == PEER_IP:
            if inet_sum(f[14:34]) != 0xFFFF:       # IPv4 header checksum must verify
                return
            sport, dport, ulen = struct.unpack(">HHH", f[34:40])
            if dport != UDP_PORT or ulen < 8:
                return
            self.ctx.send(eth(src_mac, self.mac, 0x0800, ipv4_udp(PEER_IP, f[26:30], dport, sport, f[42:34 + ulen])))
