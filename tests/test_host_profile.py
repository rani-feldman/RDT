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
    assert p.format_write(0, 0x20000000, 0x3FC00000, 4) == "MEMory:WRITE 0,20000000,3FC00000,4"
    assert p.format_read(0, 0x11AABBCCDD, 4) == "MEMory:READ? 0,11AABBCCDD,4"   # rani's example
    assert p.format_write(0, 0x100, 0xA, 1) == "MEMory:WRITE 0,100,A,1"


def test_templates_are_user_defined():
    p = Profile(read="read {mcu},{addr},{len}", write="write {mcu},{addr},{value},{size}")
    assert p.format_read(0, 4096, 16) == "read 0,1000,16"
    assert p.format_write(2, 4096, 255, 1) == "write 2,1000,FF,1"


def test_a_template_may_pad_but_not_change_the_base():
    p = Profile(read="MEM:RD? {mcu},{addr:08X},{len}", write="MEM:WR {mcu},{addr},{value:04X},{size}")
    assert p.format_read(0, 0x100, 8) == "MEM:RD? 0,00000100,8"
    assert p.format_write(0, 0x100, 0xA, 2) == "MEM:WR 0,100,000A,2"
    for template in ("MEM:RD? {mcu},{addr:d},{len}", "MEM:RD? {mcu},{addr:#X},{len}", "MEM:RD? {mcu},{addr:x},{len}"):
        with pytest.raises(ConfigError, match="bare hex"):
            Profile(read=template)


@pytest.mark.parametrize("table", [
    {"read": "MEM:RD? {mcu},{address},{len}"},   # unknown placeholder
    {"read": "MEM:RD? {mcu},{addr}"},            # no length
    {"write": "MEM:WR {mcu},{addr}"},            # no value
    {"max_write": 3},
    {"timeout": 500},                            # typo for timeout_ms
])
def test_bad_profiles_are_refused(table):
    with pytest.raises(ConfigError):
        Profile.from_dict(table)


def test_decodes_what_the_simulator_encodes():
    data = bytes(range(256))
    assert Profile().decode_data(encode_data(data), 256) == data
    assert Profile().decode_data(b"0000c03f", 4) == bytes.fromhex("0000C03F")   # either case


def test_reply_data_must_be_bare_hex():
    for reply in (b"0x0000C03F", b"00,00,C0,3F", b"#H0000C03F"):
        with pytest.raises(BadReply, match="bare hex"):
            Profile().decode_data(reply, 4)


def test_reply_size_must_match():
    with pytest.raises(BadReply):
        Profile().decode_data(b"0000C0", 4)
    with pytest.raises(BadReply):
        Profile().decode_data(b"zz00C03F", 4)


def test_reply_regex():
    p = Profile(reply_regex=r"DATA=(?P<data>[0-9A-F]+);")
    assert p.decode_data(b"OK DATA=0000C03F;", 4) == bytes.fromhex("0000C03F")
    with pytest.raises(BadReply):
        p.decode_data(b"0000C03F", 4)


def test_error_replies():
    assert parse_error('0,"No error"') is None
    e = parse_error('-240,"MCU not responding"')
    assert (e.code, e.message) == (-240, "MCU not responding")
    assert parse_error("-113").code == -113


def test_host_and_simulator_read_the_profile_the_same_way():
    host, sim = Profile.load(DEFAULT_PROFILE), SimProfile.load(DEFAULT_PROFILE)
    for key in ("read", "write", "max_write",
                "max_response", "error_query", "terminator"):
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
