# Fault-injecting TF02-Pro-style LiDAR on I2C (7-bit 0x10).
# Good-case protocol: Benewake TF02-Pro User Manual (REV 01/04/2024): command
# frame 5A len id ... cksum (cksum = low byte of the sum of all prior bytes);
# "trigger data" (5A 05 00 01 60) is answered with a 9-byte frame
# 59 59 distL distH strL strH tempL tempH cksum; the reply to the last command
# is served on the next read.
#
# Faults (the phase is the number of trigger commands the firmware has sent,
# modulo 5, so each poll gets the next fault):
#   0 valid frame, 1250 cm                  -> accept
#   1 valid frame but checksum + 1          -> reject (bad checksum)
#   2 header 59 58                          -> reject (bad header)
#   3 distance 0xFFFF, strength 0           -> out of range / no return: ignore
#   4 the identical valid frame again   -> stuck sensor: the same value for
#     every following poll in this phase; detect "never changes"
# Version query (5A 04 01 5F) replies with a frame whose ID byte is wrong
# (0xEE instead of 0x01), so a probe that checks the device identity must fail.
# I2C limits: peers cannot yet NACK a data byte or stretch SCL mid-transfer,
# so none of that is injected here.
# A robust firmware checks header and checksum, range-checks distance and
# strength, and flags a value that never changes.
GOOD_CM = 1250
TEMP_RAW = (25 + 256) * 8


def cksum(b):
    return sum(b) & 0xFF


def data_frame(dist, strength, head=b"\x59\x59", bump=0):
    body = head + dist.to_bytes(2, "little") + strength.to_bytes(2, "little") + TEMP_RAW.to_bytes(2, "little")
    return body + bytes([(cksum(body) + bump) & 0xFF])


def phase_frame(phase):
    if phase == 0:
        return data_frame(GOOD_CM, 3000)
    if phase == 1:
        return data_frame(GOOD_CM, 3000, bump=1)
    if phase == 2:
        return data_frame(GOOD_CM, 3000, head=b"\x59\x58")
    if phase == 3:
        return data_frame(0xFFFF, 0)
    return data_frame(GOOD_CM, 3000)   # stuck: identical frame every time


class Slave:
    def __init__(self, ctx):
        self.ctx = ctx
        self.reply = b""
        self.triggers = 0

    def on_write(self, data):
        data = bytes(data)
        if len(data) < 3 or data[0] != 0x5A:
            return
        if data[2] == 0x01:                       # version query: wrong ID byte
            body = bytes([0x5A, 0x07, 0xEE, 0x02, 0x03, 0x01])
            self.reply = body + bytes([cksum(body)])
        elif data[2] == 0x00:                     # trigger
            self.reply = phase_frame(self.triggers % 5)
            self.triggers += 1

    def on_read(self, count):
        return (self.reply + bytes(count))[:count]
