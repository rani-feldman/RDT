import pytest

from sim.profile import CommandPattern, Profile, parse_hex, parse_int


@pytest.mark.parametrize("line", [
    "MEMory:READ? 0,20000000,4",
    "MEM:READ? 0,20000000,4",
    "memory:read? 0,20000000,4",
    ":MEM:READ? 0 , 20000000 , 4",
])
def test_long_and_short_forms_match(line):
    p = CommandPattern("MEMory:READ? {mcu},{addr},{len}")
    assert p.match(line) == {"mcu": "0", "addr": "20000000", "len": "4"}


def test_partial_mnemonic_does_not_match():
    p = CommandPattern("MEMory:READ? {mcu},{addr},{len}")
    assert not p.match_header("MEMO:READ? 0,0,4")
    assert not p.match_header("MEM:READ 0,0,4")  # the query form needs its "?"


def test_plain_lowercase_template():
    p = CommandPattern("read {mcu},{startAddress},{size}")
    assert p.match("READ 1,4096,16") == {"mcu": "1", "startAddress": "4096", "size": "16"}


def test_header_match_but_bad_params():
    p = CommandPattern("MEMory:WRITE {mcu},{addr},{value},{size}")
    assert p.match_header("MEM:WRITE 0,1")
    assert p.match("MEM:WRITE 0,1") is None


def test_command_without_params():
    p = CommandPattern("*IDN?")
    assert p.match("*idn?") == {}
    assert p.match("*IDN? 1") is None


@pytest.mark.parametrize("text,base,value", [
    ("16", 0, 16), ("0x10", 0, 16), ("#H10", 0, 16), ("#B101", 0, 5), ("#Q17", 0, 15),
    ("0010", 0, 10), ("-1", 0, -1), ("3FC00000", 16, 0x3FC00000),
])
def test_parse_int(text, base, value):
    assert parse_int(text, base) == value


def test_addresses_and_values_are_bare_hex():
    assert parse_hex("20000000") == 0x20000000
    assert parse_hex("11aabbccdd") == 0x11AABBCCDD
    for text in ("0x20000000", "#H10", "-1", "", "12G"):
        with pytest.raises(ValueError):
            parse_hex(text)


def test_profile_ignores_host_only_keys():
    p = Profile.from_dict({"read": "R? {mcu},{addr},{len}", "timeout_ms": 500, "byte_order": "little"})
    assert p.read == "R? {mcu},{addr},{len}"
