# DroneCAN (UAVCAN v0) node as a scripted CAN peer: anonymous dynamic node-ID
# allocation, then 1 Hz NodeStatus heartbeats from the allocated ID.
# Spec: DroneCAN / UAVCAN v0.9 specification: uavcan.protocol.dynamic_node_id
# .Allocation (data type ID 1) and uavcan.protocol.NodeStatus (ID 341).
#   message id  = prio<<24 | type<<8 | src                (src 1..127)
#   anonymous   = prio<<24 | discriminator<<10 | (type&3)<<8 | 0
#   tail byte   = start<<7 | end<<6 | toggle<<5 | transfer_id (5 bits)
# Allocation is a 3-stage handshake: the node sends 6, 6 and 4 bytes of its
# 16-byte unique ID, each request carrying the preferred node ID in bits 7..1
# of byte 0 (bit 0 = "first part"); the allocator answers with the unique-ID
# bytes it has so far, and with the full 16 once it grants an ID.
# The allocator is expected to be the firmware at node ID 1.
NODESTATUS_ID = 341
ALLOCATION_ID = 1
PRIO_LOW = 24
ALLOCATOR = 1
UNIQUE_ID = bytes(range(0x50, 0x60))
PREFERRED = 42
STAGES = (UNIQUE_ID[0:6], UNIQUE_ID[6:12], UNIQUE_ID[12:16])
BOOT_DELAY_US = 3_000_000   # give the firmware time to bring its CAN controller up
RETRY_US = 1_000_000


def msg_id(dtype, src, prio=PRIO_LOW):
    return prio << 24 | dtype << 8 | src


def anon_id(dtype, discr=0x1234, prio=PRIO_LOW):
    return prio << 24 | discr << 10 | (dtype & 3) << 8


class Node:
    def __init__(self, ctx):
        self.ctx = ctx
        self.node_id = self.stage = self.tid = self.uptime = 0
        ctx.schedule_oneshot(BOOT_DELAY_US)

    def tail(self):
        t = 0xC0 | (self.tid & 0x1F)   # single-frame transfer: start|end, toggle 0
        self.tid += 1
        return t

    def on_timer(self):
        if self.node_id:
            self.uptime += 1
            # uptime u32 LE, health/mode/sub_mode byte (0 = OK/OPERATIONAL), vendor-specific u16
            payload = self.uptime.to_bytes(4, "little") + b"\x00" + b"\x00\x00"
            self.ctx.send(msg_id(NODESTATUS_ID, self.node_id), payload + bytes([self.tail()]), extended=True)
            return
        self.stage = 0
        self._send_stage()
        self.ctx.schedule_oneshot(RETRY_US)   # retry if the allocator stays silent

    def _send_stage(self):
        head = bytes([(PREFERRED << 1) | (1 if self.stage == 0 else 0)])
        self.ctx.send(anon_id(ALLOCATION_ID), head + STAGES[self.stage] + bytes([self.tail()]), extended=True)

    def on_frame(self, frame):
        cid, data = frame["id"], bytes(frame["data"])
        src, dtype = cid & 0x7F, (cid >> 8) & 0xFFFF
        if src != ALLOCATOR or dtype != ALLOCATION_ID or not data or not data[-1] & 0x80:
            return
        allocated = (data[0] if data[-1] & 0x40 else data[2]) >> 1   # first frame of a multi-frame transfer has 2 CRC bytes
        if allocated:
            self.node_id, self.uptime = allocated, 0
            self.ctx.schedule_periodic(1_000_000)
        elif self.stage < 2:
            self.stage += 1
            self._send_stage()
