"""The poll scheduler (block merging), statistics and the CSV log."""

import csv
from pathlib import Path

import pytest

from rdt import Profile
from rdt.csvlog import CsvLog, cell
from rdt.errors import CommandTimeout, SymbolError
from rdt.scheduler import plan, read_many
from rdt.stats import StatsBook
from rdt.symbols import EnumValue
from rdt.workspace import Tab, Watch, Workspace
from sim import Simulator, TransportConfig, load_config

from host_helpers import make_target

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sim():
    cfg = load_config(ROOT / "sim" / "examples" / "two_mcus.toml")
    for m in cfg.device.mcus.values():
        m.signals.clear()   # stable values
    with Simulator(cfg.device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


@pytest.fixture
def target(sim):
    with make_target("tcp", sim.tcp_address) as t:
        yield t


def counted(t, monkeypatch):
    reads = []
    real = t.read

    def read(mcu, addr, size):
        reads.append((t.mcu(mcu).name, addr, size))
        return real(mcu, addr, size)

    monkeypatch.setattr(t, "read", read)
    return reads


def blocks(t, exprs, **kw):
    return [(b.mcu, b.addr, b.size, b.items) for b in plan(t, [t.describe(e) for e in exprs], **kw)]


def test_nearby_items_share_one_read(target):
    # motor (16 bytes at 0x20000000), tick_count right after it, gains after that
    assert blocks(target, ["main:tick_count", "main:motor.speed", "main:gains[3]"]) == \
        [("main", 0x20000000, 0x24, [1, 0, 2])]
    # a struct and one of its own fields: one read
    assert blocks(target, ["main:motor", "main:motor.mode"]) == [("main", 0x20000000, 16, [0, 1])]


def test_far_apart_items_and_other_mcus_are_read_separately(target):
    got = blocks(target, ["main:motor.speed", "main:rx_buffer[199]", "motorctl:pwm_duty"])
    assert got == [("main", 0x20000000, 4, [0]), ("main", 0x20000024 + 199, 1, [1]),
                   ("motorctl", 0x20000084, 4, [2])]
    # with a larger gap allowed, the two main items merge
    assert blocks(target, ["main:motor.speed", "main:rx_buffer[199]"], gap=256)[0][2] == 0xEC


def test_blocks_stay_within_max_response(sim):
    with make_target("tcp", sim.tcp_address, Profile(timeout_ms=300, max_response=8)) as t:
        assert blocks(t, ["main:motor.speed", "main:motor.target", "main:motor.current_ma"]) == \
            [("main", 0x20000000, 8, [0, 1]), ("main", 0x20000008, 2, [2])]
        # one item larger than max_response is still one block; the session splits the read
        assert blocks(t, ["main:motor"]) == [("main", 0x20000000, 16, [0])]


def test_peripheral_items_are_never_merged(target):
    got = blocks(target, ["@main:0x48000000:u32", "@main:0x48000004:u32", "@main:0x48000002:u16"])
    assert got == [("main", 0x48000000, 4, [0]), ("main", 0x48000002, 2, [2]), ("main", 0x48000004, 4, [1])]


def test_read_many_matches_single_reads(target, monkeypatch):
    exprs = ["main:motor.mode", "motorctl:pwm_duty", "main:motor.speed", "main:tick_count", "motorctl:temperature"]
    expected = [target.read_value(e) for e in exprs]
    reads = counted(target, monkeypatch)
    samples = read_many(target, exprs)
    assert [s.expr for s in samples] == exprs
    assert [target.read_value(s.loc) for s in samples] == expected   # the same bytes as read alone
    assert all(s.error is None and s.time > 0 for s in samples)
    assert sorted(reads[:2]) == [("main", 0x20000000, 0x14), ("motorctl", 0x20000084, 8)]


def test_read_many_keeps_errors_per_item(target):
    samples = read_many(target, ["main:motor.speed", "main:no_such_thing", "@main:0x10000000:u32"])
    assert samples[0].data == bytes.fromhex("0000c03f")
    assert isinstance(samples[1].error, SymbolError)
    assert "outside" in str(samples[2].error)


def test_a_failing_merged_block_is_read_item_by_item(target, monkeypatch):
    real = target.read

    def read(mcu, addr, size):
        if size > 4:
            raise CommandTimeout("no reply")
        return real(mcu, addr, size)

    monkeypatch.setattr(target, "read", read)
    samples = read_many(target, ["main:motor.speed", "main:motor.target", "main:motor"])
    assert [s.error is None for s in samples] == [True, True, False]
    assert isinstance(samples[2].error, CommandTimeout)


def test_workspace_read_many_scales_like_read(target):
    ws = Workspace(target.config, [Tab("Watch", [
        Watch.from_dict({"expr": "main:motor.current_ma", "scale": 0.001, "unit": "A"}), Watch("main:motor")])])
    one = [ws.read(target, w, headers=True)[1] for w in ws.watches]
    many = ws.read_many(target, ws.watches, headers=True)
    assert [r.shown for r in many] == one
    assert many[0].shown[0].text == "-0.12 A" and many[0].shown[0].value == pytest.approx(-0.12)


def test_stats():
    book = StatsBook()
    for v in (1, 5, 3.0, True):
        book.add("main:a", v)
    book.add("main:a", b"text")   # not a number: not counted
    book.add("main:s.x", EnumValue(2, "TWO"))
    st = book.get("main:a")
    assert (st.n, st.min, st.max, st.mean) == (4, 1, 5, 2.5)
    assert book.get("main:s.x").mean == 2
    book.reset("main:s")
    assert book.get("main:s.x") is None and book.get("main:a") is not None
    book.reset()
    assert book.get("main:a") is None


def test_csv_log(tmp_path):
    path = tmp_path / "log.csv"
    with CsvLog(path, ["main:a", "main:b", "main:c"], {"main:a": "°C"}) as log:
        log.write(1_800_000_000.0, {"main:a": 1.5, "main:b": EnumValue(1, "RUN"), "main:other": 3})
        log.write(1_800_000_000.25, {"main:c": True})
        log.event(1_800_000_001.0, "link down")
    assert log.rows == 3
    text = path.read_bytes()
    assert text.startswith(b"\xef\xbb\xbf")   # BOM, for Excel
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == ["time", "seconds", "main:a [°C]", "main:b", "main:c", "event"]
    assert rows[1][1:] == ["0.000", "1.5", "1", "", ""]
    assert rows[2][1:] == ["0.250", "", "", "1", ""]
    assert rows[3][1:] == ["1.000", "", "", "", "link down"]
    assert rows[1][0].startswith("2027-01-15T") and rows[1][0][19:23] == ".000"
    assert (cell(2.0), cell(-3), cell(b"rdt\0\0"), cell(float("nan"))) == ("2", "-3", "rdt", "nan")
