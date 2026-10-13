"""The host core end to end against the simulated device, over every transport."""

import itertools
import socket
import sys
import threading
import time

import pytest

from rdt import (
    AccessRefused, CommandRejected, CommandTimeout, LinkDown, LinkState, McuSilent, Profile, Range,
    WriteNotConfirmed,
)
from sim import Device, Profile as SimProfile, Signal, Simulator, TransportConfig

from host_helpers import Relay, host_mcus, make_target


@pytest.fixture
def sim(device):
    with Simulator(device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


@pytest.fixture(params=["tcp", "udp"])
def target(request, sim):
    address = sim.tcp_address if request.param == "tcp" else sim.udp_address
    with make_target(request.param, address) as t:
        yield t


def test_connect_probes_the_device(target):
    assert target.link_state is LinkState.UP
    assert target.idn.startswith("RDT,Simulator")
    assert target.max_response == 1024


def test_read_values_on_each_mcu(target):
    assert target.read_value("@main:0x20000000:f32") == 1.5          # motor.speed
    assert target.read_value("@main:0x20000010:u32") == 0x12345678   # tick_count
    assert target.read_value("@main:0x20000008:i16") == -120         # motor.current_ma
    assert target.read_value("@motorctl:0x20000084:u32") == 500      # pwm_duty


def test_large_read_on_a_secondary_mcu(target, device):
    data = target.read("motorctl", 0x20000000, 140)
    assert data[:4] == bytes.fromhex("01000200")
    assert device.mcus[1].frames == 3  # the main MCU split it into 64-byte frames


def test_write_is_confirmed_by_read_back(target):
    target.write_value("@main:0x20000004:f32", 2500.25)
    assert target.read_value("@main:0x20000004:f32") == 2500.25
    target.write_value("@motorctl:0x20000084:u32", "0x2EE")
    assert target.read_value("@motorctl:0x20000084:u32") == 750
    target.write("main", 0x20000024, bytes(range(1, 11)))  # 10 bytes: split into 4 + 4 + 2
    assert target.read("main", 0x20000024, 10) == bytes(range(1, 11))


def test_access_outside_known_memory_is_refused_before_sending(target, device):
    before = len(device.errors)
    with pytest.raises(AccessRefused):
        target.read("main", 0x30000000, 4)
    with pytest.raises(AccessRefused):
        target.write("main", 0x08000000, b"\x00")   # .text is read-only
    assert len(device.errors) == before


def test_unknown_mcu(target):
    with pytest.raises(Exception, match="unknown MCU 'aux'"):
        target.read_value("@aux:0x20000000:u32")


def test_console_command(target):
    assert target.command("*IDN?").startswith("RDT,Simulator")
    assert target.command("SIM:MCU:LIST?") == '0,"main",1,"motorctl"'
    assert target.command("*CLS") is None
    assert target.command("FOO:BAR?") is None                 # failed: no reply
    assert target.error_query().code == -113


def test_timeout_gives_the_error_queue_as_a_hint(sim):
    # The host thinks 0x30000000 exists, the device does not: the read fails with no reply.
    profile = Profile(timeout_ms=100, retries=1, ranges=[Range("ghost", 0x30000000, 16)])
    with make_target("tcp", sim.tcp_address, profile) as t:
        t0 = time.perf_counter()
        with pytest.raises(CommandTimeout) as e:
            t.read("main", 0x30000000, 4)
        assert e.value.hint.code == -222
        assert time.perf_counter() - t0 < 1.0
        assert t.link_state is LinkState.UP     # the device answered the error query
        assert t.read_value("@main:0x20000010:u32") == 0x12345678


def test_write_that_does_not_land_is_reported(sim):
    profile = Profile(timeout_ms=100, retries=0, ranges=[Range("ghost", 0x30000000, 16)])
    with make_target("tcp", sim.tcp_address, profile) as t:
        with pytest.raises(WriteNotConfirmed):
            t.write("main", 0x30000000, b"\x01")


def test_write_to_a_value_the_firmware_changes_is_not_confirmed(two_mcus):
    # The clock moves on at every read, so the value never stays put; a wall clock could stand
    # still between the write and its read-back (Windows' monotonic clock ticks every ~16 ms).
    ticks = itertools.count()
    two_mcus[0].clock = lambda: float(next(ticks))
    two_mcus[0].signals.append(Signal(addr=0x20000010, type="u32", kind="counter", period_ms=1000, offset=100))
    with Simulator(Device(two_mcus), TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        with make_target("tcp", s.tcp_address) as t:
            with pytest.raises(WriteNotConfirmed) as e:
                t.write_value("@main:0x20000010:u32", 0)
            assert e.value.read_back is not None


def test_max_response_comes_from_the_profile_only(two_mcus):
    dev = Device(two_mcus, SimProfile(max_response=32), internal_timeout_ms=1)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        t = make_target("tcp", s.tcp_address, Profile(timeout_ms=300, max_response=32))
        sent = []
        send = t.session.protocol.send
        t.session.protocol.send = lambda command: (sent.append(command), send(command))[1]
        with t:
            assert t.max_response == 32
            assert sent == ["*IDN?"]          # nothing asks the device for its maximum
            assert len(t.read("main", 0x20000000, 236)) == 236   # 8 reads, none over 32 bytes


def test_addresses_values_and_replies_are_bare_hex(two_mcus):
    dev = Device(two_mcus)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        t = make_target("tcp", s.tcp_address)
        sent = []
        send = t.session.protocol.send
        t.session.protocol.send = lambda command: (sent.append(command), send(command))[1]
        with t:
            data = bytes([0x0A, 0x0D, 0x23, 0x00, 0x0A, 0x31])   # newline, CR and '#' inside the data
            t.write("main", 0x20000024, data)
            assert t.read("main", 0x20000024, 6) == data
            assert t.read_value("@main:0x20000000:f32") == 1.5
        assert "MEMory:WRITE 0,20000024,230D0A,4" in sent      # bytes 0A 0D 23 00, little endian
        assert "MEMory:READ? 0,20000024,8" in sent             # 6 bytes are read as two whole words
        assert not any("0x" in c for c in sent)


def _record(t):
    sent = []
    send = t.session.protocol.send
    t.session.protocol.send = lambda command: (sent.append(command), send(command))[1]
    return sent


def test_small_reads_ask_for_whole_aligned_words(two_mcus):
    dev = Device(two_mcus)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        t = make_target("tcp", s.tcp_address)
        sent = _record(t)
        with t:
            assert t.read_value("@main:0x20000008:i16") == -120          # 2 bytes in the word at 20000008
            assert t.read("main", 0x20000001, 2) == bytes.fromhex("00C0")
            assert t.read("main", 0x20000002, 4) == t.read("main", 0x20000000, 8)[2:6]   # straddles two words
        assert sent[1:] == ["MEMory:READ? 0,20000008,4", "MEMory:READ? 0,20000000,4",
                            "MEMory:READ? 0,20000000,8", "MEMory:READ? 0,20000000,8"]


def test_small_write_is_a_read_modify_write(two_mcus):
    dev = Device(two_mcus)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        t = make_target("tcp", s.tcp_address)
        sent = _record(t)
        with t:
            before = t.read("main", 0x20000008, 4)
            sent.clear()
            t.write_value("@main:0x2000000A:u16", 0xBEEF)
            assert t.read("main", 0x20000008, 4) == before[:2] + bytes.fromhex("EFBE")   # neighbour kept
        assert sent[:3] == ["MEMory:READ? 0,20000008,4",                                  # read the word
                            f"MEMory:WRITE 0,20000008,BEEF{before[1]:02X}{before[0]:02X},4",  # write it whole
                            "MEMory:READ? 0,20000008,4"]                                  # and confirm


def test_read_modify_write_confirms_only_the_value(two_mcus):
    """A neighbour the firmware changes between the write and the read-back is not a failure."""
    dev = Device(two_mcus)
    handle = dev.handle
    seen = []

    def firmware_changes_neighbour(line, client=""):
        if line.startswith("MEMory:READ?") and any(c.startswith("MEMory:WRITE") for c in seen):
            dev.mcus[0].write(0x20000008, b"\x55\x55")              # after the write, before the read-back
        seen.append(line)
        return handle(line, client)

    dev.handle = firmware_changes_neighbour
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        with make_target("tcp", s.tcp_address) as t:
            t.write_value("@main:0x2000000A:u16", 0x1234)
            assert t.read("main", 0x20000008, 4) == bytes.fromhex("55553412")


def test_read_replies_are_comma_separated_words(two_mcus):
    dev = Device(two_mcus)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        assert dev.handle("MEM:READ? 0,20000000,8") == b"3FC00000,453B8000"   # 1.5 and 3000.0 as uint32 values
        with make_target("tcp", s.tcp_address, Profile(timeout_ms=300)) as t:
            assert t.read_value("@main:0x20000000:f32") == 1.5
            assert t.read_value("@main:0x20000008:i16") == -120
            t.write_value("@main:0x2000000A:u16", 0xBEEF)
            assert t.read_value("@main:0x2000000A:u16") == 0xBEEF


def test_user_defined_commands(two_mcus):
    p = dict(read="RD? {mcu},{addr},{len}", write="WR {mcu},{addr},{value},{size}")
    dev = Device(two_mcus, SimProfile(**p))
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        with make_target("tcp", s.tcp_address, Profile(**p, timeout_ms=300)) as t:
            t.write_value("@motorctl:0x20000084:u16", 0xBEEF)
            assert t.read_value("@motorctl:0x20000084:u16") == 0xBEEF


def test_write_wider_than_max_write_is_split(two_mcus):
    dev = Device(two_mcus, SimProfile(max_write=2, align=2))
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        with make_target("tcp", s.tcp_address, Profile(max_write=2, align=2, timeout_ms=300)) as t:
            t.write_value("@main:0x20000004:f32", -1.25)
            assert t.read_value("@main:0x20000004:f32") == -1.25
            t.write_value("@main:0x20000028:u64", 0x0123456789ABCDEF)
            assert t.read_value("@main:0x20000028:u64") == 0x0123456789ABCDEF


def test_firmware_that_replies_with_its_error(two_mcus):
    dev = Device(two_mcus, error_reply=True)
    with Simulator(dev, TransportConfig(tcp="127.0.0.1:0", udp=None)) as s:
        profile = Profile(timeout_ms=300, ranges=[Range("ghost", 0x30000000, 16)])
        with make_target("tcp", s.tcp_address, profile) as t:
            with pytest.raises(CommandRejected) as e:
                t.read("main", 0x30000000, 4)
            assert e.value.error.code == -222


# --- silent secondary MCU ---------------------------------------------------------------------

def test_silent_mcu_is_greyed_out_alone(sim, device):
    with make_target("tcp", sim.tcp_address) as t:
        t.session.SILENT_PROBE_S = 0.2
        device.mcus[1].silent = True
        with pytest.raises(McuSilent, match="-240"):
            t.read_value("@motorctl:0x20000084:u32")
        assert t.is_silent("motorctl") and not t.is_silent("main")
        # Further accesses fail at once, without waiting for a timeout; writes are refused.
        t0 = time.perf_counter()
        with pytest.raises(McuSilent):
            t.read_value("@motorctl:0x20000084:u32")
        with pytest.raises(McuSilent):
            t.write_value("@motorctl:0x20000084:u32", 1)
        assert time.perf_counter() - t0 < 0.1
        assert t.read_value("@main:0x20000010:u32") == 0x12345678   # MCU 0 still works

        device.mcus[1].silent = False
        time.sleep(0.25)
        assert t.read_value("@motorctl:0x20000084:u32") == 500
        assert not t.is_silent("motorctl")


def test_silent_mcu_after_three_timeouts_without_an_error_query(sim, device):
    profile = Profile(timeout_ms=100, retries=2, error_query=None)
    with make_target("tcp", sim.tcp_address, profile) as t:
        device.mcus[1].silent = True
        with pytest.raises(McuSilent, match="3 timeouts in a row"):
            t.read_value("@motorctl:0x20000084:u32")
        assert t.read_value("@main:0x20000010:u32") == 0x12345678


def test_silent_mcu_after_three_timeouts_while_mcu0_answers(sim, device):
    profile = Profile(timeout_ms=100, retries=2)
    with make_target("tcp", sim.tcp_address, profile) as t:
        # The error query answers but says nothing useful, as when another tool emptied the queue.
        t.session.protocol.error_query = lambda timeout: (True, None)
        device.mcus[1].silent = True
        with pytest.raises(McuSilent, match="3 timeouts in a row"):
            t.read_value("@motorctl:0x20000084:u32")
        assert t.read_value("@main:0x20000010:u32") == 0x12345678


def test_per_mcu_timeout(sim):
    with make_target("tcp", sim.tcp_address, mcus=host_mcus(motorctl=900)) as t:
        assert t.session.timeout_s(0) == 0.3
        assert t.session.timeout_s(1) == 0.9


# --- link loss --------------------------------------------------------------------------------

def test_link_loss_and_reconnect(sim):
    relay = Relay(sim.tcp_address)
    try:
        t = make_target("tcp", relay.address)
        t.session.RECONNECT_DELAYS = (0.05, 0.1)
        states = []
        t.session.add_listener(lambda ev, info: ev == "link" and states.append(info["state"]))
        with t:
            assert t.read_value("@main:0x20000010:u32") == 0x12345678
            relay.cut()
            with pytest.raises(LinkDown):
                t.read_value("@main:0x20000010:u32")
            assert t.link_state is LinkState.DOWN
            with pytest.raises(LinkDown):
                t.write_value("@main:0x20000004:f32", 1.0)   # writes refused while down
            time.sleep(0.3)
            assert t.link_state is LinkState.DOWN           # still cut
            relay.heal()
            assert t.session.wait_until_up(3)
            assert t.read_value("@main:0x20000010:u32") == 0x12345678
        assert states == [LinkState.UP, LinkState.DOWN, LinkState.UP, LinkState.CLOSED]
    finally:
        relay.close()


def test_connect_fails_cleanly_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    t = make_target("tcp", f"127.0.0.1:{port}")
    with pytest.raises(Exception, match="tcp 127.0.0.1"):
        t.connect()
    assert t.link_state is LinkState.CLOSED


def test_udp_device_that_stops_answering_takes_the_link_down(sim):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))   # a UDP port that never answers
        t = make_target("udp", s.getsockname(), Profile(timeout_ms=50, retries=1, probe=None),
                        auto_reconnect=False)
        with t:
            with pytest.raises(LinkDown, match="stopped answering"):
                t.read_value("@main:0x20000010:u32")


# --- UDP --------------------------------------------------------------------------------------

def test_udp_retries_lost_datagrams(device):
    with Simulator(device, TransportConfig(tcp=None, udp="127.0.0.1:0", udp_drop=0.3), seed=7) as s:
        with make_target("udp", s.udp_address, Profile(timeout_ms=60, retries=5)) as t:
            for i in range(10):
                assert t.read_value("@main:0x20000010:u32") == 0x12345678
                t.write_value("@main:0x20000024:u32", i)
                assert t.read_value("@main:0x20000024:u32") == i


# --- serial -----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Linux only")
def test_serial(device):
    with Simulator(device, TransportConfig(tcp=None, udp=None, serial="pty")) as s:
        with make_target("serial", s.serial_port) as t:
            assert t.idn.startswith("RDT,Simulator")
            assert t.read_value("@motorctl:0x20000084:u32") == 500
            t.write_value("@motorctl:0x20000088:f32", 42.5)
            assert t.read_value("@motorctl:0x20000088:f32") == 42.5
            t.send_break(50)                       # CON-10: the link still works after a break
            assert t.read_value("@motorctl:0x20000084:u32") == 500
            t.reconnect()                          # CON-9
            assert t.idn.startswith("RDT,Simulator") and t.read_value("@motorctl:0x20000084:u32") == 500


# --- sharing the link -------------------------------------------------------------------------

def test_another_scpi_client_on_the_same_device(sim):
    """A second tool reads, writes, sends bad commands and empties the shared error queue while
    RDT works. Each side must only see its own replies, and RDT's results must be right."""
    stop = threading.Event()
    other = {"replies": 0, "wrong": 0}

    def other_tool():
        with socket.create_connection(sim.tcp_address, timeout=2) as sock:
            f = sock.makefile("rwb")
            while not stop.is_set():
                f.write(b"MEM:READ? 1,20000084,4\n")
                f.write(b"BOGUS:CMD 1\n")                      # fails: no reply, error queued
                f.write(b"MEM:READ? 0,7FFFFFF0,4\n")          # fails: no reply, error queued
                f.write(b"SYST:ERR?\n")                         # may pop RDT's error, or its own
                f.flush()
                for expect in (b"000001F4", None):
                    line = f.readline().rstrip(b"\n")
                    other["replies"] += 1
                    if expect is not None and line != expect:
                        other["wrong"] += 1
                    elif expect is None and not line.startswith((b"-", b"0,")):
                        other["wrong"] += 1

    th = threading.Thread(target=other_tool, daemon=True)
    th.start()
    try:
        profile = Profile(timeout_ms=200, ranges=[Range("ghost", 0x30000000, 4)])
        with make_target("tcp", sim.tcp_address, profile) as t:
            for i in range(50):
                assert t.read_value("@main:0x20000010:u32") == 0x12345678
                t.write_value("@main:0x20000024:u32", i)
                assert t.read_value("@main:0x20000024:u32") == i
                assert t.read("motorctl", 0x20000000, 16)[:2] == b"\x01\x00"
            # A failed read still fails cleanly, whatever the shared queue returns as its hint.
            with pytest.raises(CommandTimeout):
                t.read("main", 0x30000000, 4)
            assert t.link_state is LinkState.UP
    finally:
        stop.set()
        th.join(timeout=3)
    assert other["replies"] > 0 and other["wrong"] == 0
