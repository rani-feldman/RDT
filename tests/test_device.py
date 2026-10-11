import time

import pytest

from sim import Device, Profile
from conftest import ram_mcu


def q(device, line):
    reply = device.handle(line)
    return None if reply is None else reply.decode("latin-1")


def test_identity_and_max(device):
    assert q(device, "*IDN?").startswith("RDT,Simulator")
    assert q(device, "MEM:MAX?") == "1024"
    assert q(device, "SIM:MCU:LIST?") == '0,"main",1,"motorctl"'


def test_read_each_mcu(device):
    assert q(device, "MEM:READ? 0,0x20000000,4") == "0000C03F"   # main: motor.speed = 1.5f
    assert q(device, "MEM:READ? 1,0x20000084,4") == "F4010000"   # motorctl: pwm_duty = 500


def test_large_read_from_secondary_is_split_into_frames(device):
    reply = q(device, "MEM:READ? 1,0x20000000,140")
    assert len(reply) == 280
    assert device.mcus[1].frames == 3


def test_write_then_read_back(device):
    assert q(device, "MEM:WRITE 0,0x20000000,0x40490FDB,4") is None
    assert q(device, "MEM:READ? 0,0x20000000,4") == "DB0F4940"
    assert q(device, "SYST:ERR?") == '0,"No error"'


def test_write_negative_value_is_twos_complement(device):
    q(device, "MEM:WRITE 1,0x20000084,-1,2")
    assert q(device, "MEM:READ? 1,0x20000084,4") == "FFFF0000"


@pytest.mark.parametrize("line,code", [
    ("MEM:READ? 0,0x30000000,4", -222),        # unmapped
    ("MEM:READ? 0,0x20000000,1025", -223),     # over max_response
    ("MEM:READ? 0,0x20000000,0", -224),
    ("MEM:READ? 9,0x20000000,4", -224),        # unknown MCU
    ("MEM:READ? 0,banana,4", -104),
    ("MEM:READ? 0,0x20000000", -102),
    ("FOO:BAR?", -113),
])
def test_query_errors_reply_and_queue(device, line, code):
    assert q(device, line).startswith(f"{code},")
    assert q(device, "SYST:ERR?").startswith(f"{code},")
    assert q(device, "SYST:ERR?") == '0,"No error"'


@pytest.mark.parametrize("line,code", [
    ("MEM:WRITE 0,0x08000050,1,1", -222),     # flash is read-only
    ("MEM:WRITE 0,0x20000000,0x100,1", -222),  # does not fit
    ("MEM:WRITE 0,0x20000000,1,8", -224),      # wider than max_write
    ("MEM:WRITE 0,0x20000000,1,3", -224),
])
def test_write_errors_only_queue(device, line, code):
    assert q(device, line) is None
    assert q(device, "SYST:ERR?").startswith(f"{code},")


def test_silent_secondary_mcu(device):
    device.internal_timeout_ms = 20
    q(device, "SIM:MCU:SIL 1,ON")
    assert q(device, "SIM:MCU:SIL? 1") == "1"
    start = time.perf_counter()  # time.monotonic ticks every ~16 ms on Windows
    assert q(device, "MEM:READ? 1,0x20000084,4") == '-240,"MCU not responding"'
    assert time.perf_counter() - start >= 0.018  # the main MCU waited out its internal timeout
    assert q(device, "MEM:READ? 0,0x20000000,4") == "0000C03F"  # the others keep working
    q(device, "SIM:MCU:SIL 1,OFF")
    assert q(device, "MEM:READ? 1,0x20000084,4") == "F4010000"


def test_main_mcu_cannot_be_silent(device):
    q(device, "SIM:MCU:SIL 0,ON")
    assert q(device, "SYST:ERR?").startswith("-224,")


def test_reset_restores_memory(device):
    q(device, "MEM:WRITE 0,0x20000000,0,4")
    q(device, "*RST")
    assert q(device, "MEM:READ? 0,0x20000000,4") == "0000C03F"


def test_error_queue_overflow():
    d = Device([ram_mcu()])
    for _ in range(20):
        d.handle("NOPE")
    errors = [q(d, "SYST:ERR?") for _ in range(17)]
    assert errors[15].startswith("-350,")
    assert errors[16] == '0,"No error"'


def test_errors_can_stay_silent():
    d = Device([ram_mcu()], error_reply=False)
    assert d.handle("MEM:READ? 0,0x30000000,4") is None
    assert q(d, "SYST:ERR?").startswith("-222,")


@pytest.mark.parametrize("fmt,expected", [
    ("hex", b"0000C03F"),
    ("bytes_csv", b"00,00,C0,3F"),
    ("decimal", b"0,0,192,63"),
    ("ieee_block", b"#14\x00\x00\xc0\x3f"),
])
def test_data_formats(two_mcus, fmt, expected):
    d = Device(two_mcus, Profile(data_format=fmt))
    assert d.handle("MEM:READ? 0,0x20000000,4") == expected


def test_user_defined_templates():
    """The commands rani's firmware will have: plain read/write with bare hex values."""
    profile = Profile(read="read {mcu},{addr},{len}", write="write {mcu},{addr},{value},{size}",
                      addr_format="{addr:X}", value_format="{value:X}", size_query=None)
    d = Device([ram_mcu(0), ram_mcu(2)], profile)
    assert d.handle("write 2,20000010,DEADBEEF,4") is None
    assert d.handle("read 2,20000010,4") == b"EFBEADDE"
    assert q(d, "SYST:ERR?") == '0,"No error"'
    assert d.handle("read 2,30000000,4").startswith(b"-222,")  # read replies even without "?"


def test_big_endian_mcu():
    d = Device([ram_mcu(0, byte_order="big")])
    d.handle("MEM:WRITE 0,0x20000000,0x1234,2")
    assert d.handle("MEM:READ? 0,0x20000000,2") == b"1234"
