import pytest

from rdt import BadReply, ConfigError, Profile
from rdt.profile import parse_error
from sim import Profile as SimProfile
from sim.device import encode_data

from host_helpers import FIRMWARE

DEFAULT_PROFILE = FIRMWARE.parent / "default_profile.toml"


def test_default_commands_use_bare_hex():
    p = Profile()
    assert p.format_read(1, 0x20000084, 4) == "MEMory:READ? 1,20000084,4"
    assert p.format_read(0, 0x08000000, 4) == "MEMory:READ? 0,08000000,4"   # leading zero kept (rani)
    assert p.format_write(0, 0x20000000, 0x3FC00000) == "MEMory:WRITE 0,20000000,3FC00000"
    assert p.format_read(0, 0x11AABBCCDD, 4) == "MEMory:READ? 0,11AABBCCDD,4"   # rani's example
    assert p.format_write(0, 0x100, 0xA) == "MEMory:WRITE 0,00000100,0000000A"   # addresses always 8 digits


def test_templates_are_user_defined():
    p = Profile(read="read {mcu},{addr},{len}", write="write {mcu},{addr},{value}")
    assert p.format_read(0, 4096, 16) == "read 0,00001000,16"
    assert p.format_write(2, 4096, 255) == "write 2,00001000,000000FF"


def test_a_template_may_not_change_the_base_or_the_padding():
    p = Profile(read="MEM:RD? {mcu},{addr:08X},{len}", write="MEM:WR {mcu},{addr},{value:08X}")
    assert p.format_read(0, 0x100, 8) == "MEM:RD? 0,00000100,8"
    assert p.format_write(0, 0x100, 0xA) == "MEM:WR 0,00000100,0000000A"
    with pytest.raises(ConfigError, match="8 zero-padded"):    # a value is always 8 digits (SCPI-14)
        Profile(write="MEM:WR {mcu},{addr},{value:04X}")
    for template in ("MEM:RD? {mcu},{addr:d},{len}", "MEM:RD? {mcu},{addr:#X},{len}", "MEM:RD? {mcu},{addr:x},{len}"):
        with pytest.raises(ConfigError, match="bare hex"):
            Profile(read=template)



@pytest.mark.parametrize("template", ["MEM:RD? {mcu},{addr:8X},{len}", "MEM:RD? {mcu},{addr:X},{len}",
                                      "MEM:RD? {mcu},{addr:04X},{len}", "MEM:RD? {mcu},{addr:010X},{len}"])
def test_an_address_is_always_8_zero_padded_digits(template):
    """L5: {addr:8X} pads with spaces, {addr:X} or {addr:04X} gives fewer than 8 digits and
    {addr:010X} gives 10; the firmware rejects all of them, so the profile is refused."""
    with pytest.raises(ConfigError):
        Profile(read=template)
    with pytest.raises(ConfigError, match="zero-padded"):
        Profile(write="MEM:WR {mcu},{addr},{value:4X}")
    assert Profile(read="R? {mcu},{addr:08X},{len}").format_read(0, 0x100, 4) == "R? 0,00000100,4"


@pytest.mark.parametrize("table", [
    {"read": "MEM:RD? {mcu},{address},{len}"},   # unknown placeholder
    {"read": "MEM:RD? {mcu},{addr}"},            # no length
    {"write": "MEM:WR {mcu},{addr}"},            # no value
    {"max_write": 3},
    {"align": 3},
    {"max_write": 2},                            # narrower than the 4-byte align
    {"max_response": 2},
    {"timeout": 500},                            # typo for timeout_ms
    {"timeout_ms": "500"},                       # L6: wrong value types are config errors too
    {"retries": 1.5},
    {"max_response": True},
    {"read": 5},
])
def test_bad_profiles_are_refused(table):
    with pytest.raises(ConfigError):
        Profile.from_dict(table)


def test_decodes_what_the_simulator_encodes():
    data = bytes(range(256))
    assert Profile().decode_data(encode_data(data), 256) == data
    assert Profile().decode_data(b"3fc00000", 4) == bytes.fromhex("0000C03F")   # either case


def test_reply_data_must_be_bare_hex():
    for reply in (b"0x3FC00000", b"0000C03F00", b"#H3FC00000"):
        with pytest.raises(BadReply, match="bare hex"):
            Profile().decode_data(reply, 4)


def test_reply_size_must_match():
    with pytest.raises(BadReply):
        Profile().decode_data(b"3FC00000,00000001", 4)
    with pytest.raises(BadReply):
        Profile().decode_data(b"zz00C03F", 4)


def test_retired_keys_are_ignored():
    p = Profile.from_dict({"reply_regex": "DATA=(.*)", "error_query": "SYST:ERR?", "probe": "*IDN?"})
    assert p.decode_data(b"3FC00000", 4) == bytes.fromhex("0000C03F")


def test_error_replies():
    assert parse_error('0,"No error"') is None
    e = parse_error('-240,"MCU not responding"')
    assert (e.code, e.message) == (-240, "MCU not responding")
    assert parse_error("-113").code == -113


def test_host_and_simulator_read_the_profile_the_same_way():
    host, sim = Profile.load(DEFAULT_PROFILE), SimProfile.load(DEFAULT_PROFILE)
    for key in ("read", "write", "max_write", "align",
                "max_response", "terminator"):
        assert getattr(host, key) == getattr(sim, key), key
    assert host.timeout_ms == 500


def test_profile_ranges(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text('[scpi]\nname = "x"\n\n[[ranges]]\nname = "GPIOA"\nstart = 0x48000000\nsize = 0x400\n'
                 '\n[[ranges]]\nname = "ADC"\nstart = 0x50000000\nsize = 16\nwritable = false\nmcu = "motorctl"\n')
    p = Profile.load(f)
    assert [(r.name, r.start, r.size, r.writable, r.mcu) for r in p.ranges] == [
        ("GPIOA", 0x48000000, 0x400, True, None), ("ADC", 0x50000000, 16, False, "motorctl")]


def test_an_old_size_query_key_is_ignored():
    p = Profile.from_dict({"size_query": "MEMory:MAX?", "max_response": 256})
    assert p.max_response == 256 and not hasattr(p, "size_query")


def test_old_format_keys_are_ignored():
    p = Profile.from_dict({"addr_format": "0x{addr:08X}", "value_format": "0x{value:X}", "data_format": "hex"})
    assert p.format_read(0, 0x20000000, 4) == "MEMory:READ? 0,20000000,4"


def test_word_replies():
    """rani's firmware (2026-10-13) prints each 4-byte word as a uint32 value, comma-separated."""
    import struct
    p = Profile()
    data = p.decode_data(b"30303030,2F30302F,06003030,00000101,43B40000", 20, "little")
    assert data[:12] == b"0000/00/00\x00\x06"                 # a date string, in memory order
    assert struct.unpack("<I", data[12:16])[0] == 0x101
    assert struct.unpack("<f", data[16:20])[0] == 360.0
    assert p.decode_data(b"43B40000", 4, "big") == bytes.fromhex("43B40000")
    assert p.decode_data(b"1, 2", 8, "little") == bytes.fromhex("0100000002000000")   # spaces, no padding
    for reply in (b"30303030,", b"0x43B40000", b"30303030 2F30302F", b"123456789"):
        with pytest.raises(BadReply):
            p.decode_data(reply, 4, "little")
    with pytest.raises(BadReply, match="expected 8 bytes"):
        p.decode_data(b"30303030", 8, "little")


def test_max_write_and_align_are_at_most_4_bytes():
    """The firmware is 32-bit, so 8 was removed for both (rani, 2026-10-21, SCPI-13)."""
    for n in (1, 2, 4):
        Profile(max_write=4, align=n)
    with pytest.raises(ConfigError, match="max_write must be 1, 2 or 4"):
        Profile(max_write=8)
    with pytest.raises(ConfigError, match="align must be 1, 2 or 4"):
        Profile(align=8, max_write=4)


def test_write_template_has_no_size():
    """Every write is one max_write-byte word, so {size} was removed (rani, 2026-10-21, SCPI-13)."""
    with pytest.raises(ConfigError, match="size.*was removed"):
        Profile(write="MEM:WR {mcu},{addr},{value},{size}")


def test_an_older_profile_still_loads_without_size():
    """A profile saved before {size} was removed opens with it taken out, so the workspace and its
    MCUs still load (rani, 2026-10-21: the hard error kept the workspace from opening)."""
    p = Profile.from_dict({"write": "SERV:MEM:WRIT {mcu},{addr},{value},{size}", "max_write": 8, "align": 8})
    assert p.write == "SERV:MEM:WRIT {mcu},{addr},{value}"
    assert (p.max_write, p.align) == (4, 4)
    assert len(p.notes) == 3 and "size" in p.notes[0]
    assert Profile.from_dict({}).notes == []


def test_a_value_is_the_whole_word_zero_padded():
    """rani, 2026-10-21 (SCPI-14): a value is 8 characters like the address, so 2FF is 000002FF."""
    assert Profile().format_write(0, 0x24062BC0, 0x2FF) == "MEMory:WRITE 0,24062BC0,000002FF"
    assert Profile(max_write=2, align=2).format_write(0, 0x100, 0xA) == "MEMory:WRITE 0,00000100,0000000A"
    old = Profile.from_dict({"write": "W {mcu},{addr},{value:X}"})       # unpadded in an older profile
    assert old.write == "W {mcu},{addr},{value}" and old.notes
