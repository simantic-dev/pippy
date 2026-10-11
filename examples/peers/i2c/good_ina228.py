# TI INA228 power monitor as a scripted I2C peer (7-bit 0x45 on A0/A1 = GND).
# Source: TI INA228 datasheet SLYS021A (rev. May 2022).
# Register file with an 8-bit pointer. A write arrives as [reg] then [data...]
# in one or two on_write calls and on_finish at STOP: accumulate, byte 0 is
# the pointer.
#
# Timing follows what the firmware programs: the conversion cycle is the sum of
# the enabled inputs' conversion times x averaging, from ADC_CONFIG; CURRENT
# and POWER scale from the SHUNT_CAL it writes (CURRENT_LSB, Eq. 2). Results
# read 0 until the first cycle completes; reading DIAG_ALRT clears CNVRF.
# ENERGY/CHARGE (40 bit, Eq. 6-7) accumulate from the last ADC_CONFIG write;
# RSTACC is not modelled.
PACK_V = 16.8      # V, bus voltage
LOAD_A = 12.0      # A, current through the shunt
DIE_C = 35.0       # degC
R_SHUNT = 0.0005   # ohm; the firmware's shunt value

CT_US = (50, 84, 150, 280, 540, 1052, 2074, 4120)  # per VBUSCT/VSHCT/VTCT code: SLYS021A Table 7-6
AVG_N = (1, 4, 16, 64, 128, 256, 512, 1024)        # per AVG code: Table 7-6
CONVDLY_STEP_US = 2000                             # CONFIG.CONVDLY step: Table 7-5

CONFIG, ADC_CONFIG, SHUNT_CAL, VBUS, DIETEMP, CURRENT, POWER, ENERGY, CHARGE, DIAG_ALRT = 0, 1, 2, 5, 6, 7, 8, 9, 10, 11
RESET = {CONFIG: 0x0000, ADC_CONFIG: 0xFB68, SHUNT_CAL: 0x1000}  # SLYS021A 7.6.1.1-3


def be(v, n):
    return (int(v) & ((1 << (8 * n)) - 1)).to_bytes(n, "big")


class Slave:
    def __init__(self, ctx):
        self.ctx = ctx
        self.ptr = 0
        self.buf = b""
        self._reset()

    def _reset(self):
        self.regs = dict(RESET)
        self.t0 = self.ctx.now_us
        self.seen = 0

    def _cycle_us(self):
        a = self.regs[ADC_CONFIG]
        ct = (CT_US[(a >> 9) & 7], CT_US[(a >> 6) & 7], CT_US[(a >> 3) & 7])  # bus, shunt, temp
        return sum(t for bit, t in zip((1, 2, 4), ct) if (a >> 12) & bit) * AVG_N[a & 7]

    def _cycles(self):
        mode, cyc = self.regs[ADC_CONFIG] >> 12, self._cycle_us()
        if mode & 7 == 0 or cyc == 0:
            return 0
        start = self.t0 + ((self.regs[CONFIG] >> 6) & 0xFF) * CONVDLY_STEP_US
        n = max(0, (self.ctx.now_us - start) // cyc)
        return n if mode & 8 else min(n, 1)   # triggered modes convert once

    def _current_lsb(self):
        adcrange = (self.regs[CONFIG] >> 4) & 1
        return (self.regs[SHUNT_CAL] & 0x7FFF) / (13107.2e6 * R_SHUNT * (4 if adcrange else 1))  # Eq. 2

    def _value(self, reg):
        n = self._cycles()
        if reg == DIAG_ALRT:
            cnvrf = n > self.seen
            self.seen = n
            return be(0x0001 | cnvrf << 1, 2)   # MEMSTAT=1, CNVRF
        if reg in RESET:
            return be(self.regs[reg], 2)
        lsb = self._current_lsb()
        if n == 0 or (reg in (CURRENT, POWER, ENERGY, CHARGE) and lsb == 0):
            return bytes({DIETEMP: 2, ENERGY: 5, CHARGE: 5}.get(reg, 3))
        t_s = n * self._cycle_us() * 1e-6   # time since conversions started
        if reg == VBUS:
            return be(round(PACK_V / 195.3125e-6) << 4, 3)   # 195.3125 uV/LSB, 20 bit left-aligned
        if reg == DIETEMP:
            return be(int(DIE_C / 7.8125e-3), 2)           # 7.8125 mdegC/LSB
        if reg == CURRENT:
            return be(round(LOAD_A / lsb) << 4, 3)           # Eq. 4
        if reg == POWER:
            return be(PACK_V * LOAD_A / (3.2 * lsb), 3)    # Eq. 5
        if reg == ENERGY:
            return be(PACK_V * LOAD_A * t_s / (16 * 3.2 * lsb), 5)   # Eq. 6, 40 bit
        if reg == CHARGE:
            return be(LOAD_A * t_s / lsb, 5)                          # Eq. 7, 40 bit
        return bytes(2)

    def on_write(self, data):
        self.buf += bytes(data)
        if not self.buf:
            return
        self.ptr = self.buf[0]
        if len(self.buf) < 3 or self.ptr not in RESET:
            return
        v = int.from_bytes(self.buf[1:3], "big")
        if self.ptr == CONFIG and v & 0x8000:        # RST
            self._reset()
            return
        if self.ptr == ADC_CONFIG:                   # writing MODE restarts conversions
            self.t0, self.seen = self.ctx.now_us, 0
        self.regs[self.ptr] = v & 0x7FFF if self.ptr == CONFIG else v

    def on_finish(self):
        self.buf = b""

    def on_read(self, count):
        self.buf = b""
        return (self._value(self.ptr) + bytes(count))[:count]
