import pytest

from rdt import BadReply, ConfigError, Profile
from rdt.profile import parse_error, parse_ieee_block
from sim import Profile as SimProfile
from sim.device import encode_data

from host_helpers import FIRMWARE

DEFAULT_PROFILE = FIRMWARE.parent / "default_profile.toml"


def test_default_commands():
    p = Profile()
    assert p.format_read(1, 0x20000084, 4) == "MEMory:READ? 1,0x20000084,4"
    assert p.format_write(0, 0x20000000, 0x3FC00000, 4) == "MEMory:WRITE 0,0x20000000,0x3FC00000,4"


def test_templates_are_user_defined():
    p = Profile(read="read {mcu},{addr},{len}", write="write {mcu},{addr},{value},{size}",
                addr_format="{addr}", value_format="{value:X}")
    assert p.format_read(0, 4096, 16) == "read 0,4096,16"
    assert p.format_write(2, 4096, 255, 1) == "write 2,4096,FF,1"


def test_a_spec_in_the_template_wins_over_the_format():
    p = Profile(read="MEM:RD? {mcu},#H{addr:X},{len}")
    assert p.format_read(0, 0x20000000, 8) == "MEM:RD? 0,#H20000000,8"


@pytest.mark.parametrize("table", [
    {"read": "MEM:RD? {mcu},{address},{len}"},   # unknown placeholder
    {"read": "MEM:RD? {mcu},{addr}"},            # no length
    {"write": "MEM:WR {mcu},{addr}"},            # no value
    {"data_format": "base64"},
    {"max_write": 3},
    {"timeout": 500},                            # typo for timeout_ms
    {"addr_format": "0x{addr:Q}"},
])
def test_bad_profiles_are_refused(table):
    with pytest.raises(ConfigError):
        Profile.from_dict(table)


@pytest.mark.parametrize("fmt", ["hex", "bytes_csv", "decimal", "ieee_block"])
def test_decodes_what_the_simulator_encodes(fmt):
    data = bytes(range(256))
    assert Profile(data_format=fmt).decode_data(encode_data(data, fmt), 256) == data


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


def test_ieee_block_checks_its_length():
    assert parse_ieee_block(b"#14\x00\n\x0a\x01") == b"\x00\n\n\x01"
    with pytest.raises(BadReply):
        parse_ieee_block(b"#18\x00\x01")


def test_error_replies():
    assert parse_error('0,"No error"') is None
    e = parse_error('-240,"MCU not responding"')
    assert (e.code, e.message) == (-240, "MCU not responding")
    assert parse_error("-113").code == -113


def test_host_and_simulator_read_the_profile_the_same_way():
    host, sim = Profile.load(DEFAULT_PROFILE), SimProfile.load(DEFAULT_PROFILE)
    for key in ("read", "write", "value_format", "addr_format", "max_write", "data_format",
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
