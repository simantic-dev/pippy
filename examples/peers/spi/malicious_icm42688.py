# Fault-injecting ICM-42688-P-like SPI peer. Follows the register framing of
# good_icm42688.py (TDK DS-000347 rev 1.2: beat 0 = register byte, bit7 =
# read; beats 1.. = data) but is deliberately small and self-contained.
#
# Faults, all deterministic:
#   WHO_AM_I (0x75)      reads 0x42 instead of 0x47     -> the driver must
#                        refuse to bring the part up.
#   FIFO_COUNTH/L        always reports FIFO_COUNT = 4 records, but FIFO_DATA
#                        returns empty packets (header 0x80 = FIFO empty,
#                        DS-000347 6.2) -> the driver must not treat them as
#                        samples; it must stop reading and not loop forever.
#   GYRO_DATA_X1 (0x25)  returns the stuck value 0x1234 on all three axes,
#                        every read -> a plausibility/"never changes" check.
#   ACCEL_DATA_X1 (0x1F) returns a real-looking sample with bit 14 of each
#                        axis flipped on every second read -> a plausibility
#                        or consecutive-sample check.
# A robust driver checks WHO_AM_I, validates FIFO headers, range-checks and
# change-checks samples, and has a bounded read loop.
WHO_AM_I, FIFO_COUNTH, FIFO_DATA, GYRO_DATA_X1, ACCEL_DATA_X1 = 0x75, 0x2E, 0x30, 0x25, 0x1F
BAD_WHO_AM_I = 0x42
FIFO_COUNT = 4
STUCK_GYRO = 0x1234
ACCEL_G_LSB = 2048  # +-16 g: 2048 LSB/g; stationary, 1 g on Z


class Slave:
    def __init__(self, ctx):
        self.ctx = ctx
        self.beat, self.reg, self.read, self.out = 0, 0, False, b""
        self.accel_reads = 0
        self.logged = set()

    def _value(self, reg):
        if reg == WHO_AM_I:
            return bytes([BAD_WHO_AM_I])
        if reg == FIFO_COUNTH:
            return FIFO_COUNT.to_bytes(2, "big")
        if reg == GYRO_DATA_X1:
            return STUCK_GYRO.to_bytes(2, "big") * 3
        if reg == ACCEL_DATA_X1:
            self.accel_reads += 1
            z = ACCEL_G_LSB ^ (0x4000 if self.accel_reads % 2 == 0 else 0)
            return (0).to_bytes(2, "big") * 2 + z.to_bytes(2, "big")
        return b"\x00"

    def on_transmit(self, b):
        i = self.beat
        self.beat += 1
        if i == 0:
            self.read, self.reg = bool(b & 0x80), b & 0x7F
            if self.read:
                self.out = self._value(self.reg)
                if self.reg in (WHO_AM_I, FIFO_COUNTH) and self.reg not in self.logged:
                    self.logged.add(self.reg)
                    self.ctx.info(f"read reg 0x{self.reg:02x}: injecting fault")
            return 0x00
        if not self.read:
            return 0x00
        if self.reg == FIFO_DATA:
            return 0x80 if (i - 1) % 16 == 0 else 0x00   # empty packet
        return self.out[i - 1] if i - 1 < len(self.out) else 0x00

    def on_finish(self):
        self.beat = 0
