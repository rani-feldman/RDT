import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from sim import Simulator, TransportConfig, load_config
from sim.transports import LineSplitter

EXAMPLE = Path(__file__).resolve().parent.parent / "sim" / "examples" / "two_mcus.toml"


@pytest.fixture
def sim(device):
    with Simulator(device, TransportConfig(tcp="127.0.0.1:0", udp="127.0.0.1:0")) as s:
        yield s


def tcp_client(sim):
    sock = socket.create_connection(sim.tcp_address, timeout=2)
    return sock, sock.makefile("rwb")


def ask(f, line):
    f.write(line.encode() + b"\n")
    f.flush()
    return f.readline().rstrip(b"\n").decode()


def test_line_splitter_handles_crlf_and_partial_lines():
    s = LineSplitter("\n")
    assert s.feed(b"*IDN?\r\nMEM:RE") == ["*IDN?"]
    assert s.feed(b"AD? 0,0,4\n") == ["MEM:READ? 0,0,4"]


def test_tcp(sim):
    sock, f = tcp_client(sim)
    with sock:
        assert ask(f, "*IDN?").startswith("RDT,Simulator")
        assert ask(f, "MEM:READ? 1,20000084,4") == "000001F4"


def test_tcp_clients_get_their_own_replies(sim):
    """Two tools share the device; each must only ever see the replies to its own commands."""
    results = {}

    def client(name, mcu, addr, expected):
        sock, f = tcp_client(sim)
        with sock:
            results[name] = all(ask(f, f"MEM:READ? {mcu},{addr},4") == expected for _ in range(50))

    threads = [threading.Thread(target=client, args=("a", 0, "20000000", "3FC00000")),
               threading.Thread(target=client, args=("b", 1, "20000084", "000001F4"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"a": True, "b": True}


def test_tcp_commands_split_across_packets(sim):
    sock, f = tcp_client(sim)
    with sock:
        sock.sendall(b"MEM:READ? 0,2000")
        time.sleep(0.05)
        sock.sendall(b"0000,4\n*OPC?\n")
        assert f.readline() == b"3FC00000\n"
        assert f.readline() == b"1\n"


def test_udp_replies_to_the_sender(sim):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(2)
        u.sendto(b"MEM:READ? 0,20000000,4\n", sim.udp_address)
        assert u.recvfrom(2048)[0] == b"3FC00000\n"
        u.sendto(b"*IDN?", sim.udp_address)  # no terminator needed in a datagram
        assert u.recvfrom(2048)[0].startswith(b"RDT,Simulator")


def test_udp_drop(device):
    t = TransportConfig(tcp=None, udp="127.0.0.1:0", udp_drop=0.5)
    with Simulator(device, t, seed=1) as s, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.settimeout(0.2)
        answered = 0
        for _ in range(20):
            u.sendto(b"*OPC?\n", s.udp_address)
            try:
                u.recvfrom(64)
                answered += 1
            except socket.timeout:
                pass
        assert 0 < answered < 20


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are Linux only")
def test_serial_pty(device):
    import serial

    with Simulator(device, TransportConfig(tcp=None, udp=None, serial="pty")) as s:
        with serial.Serial(s.serial_port, 115200, timeout=2) as port:
            port.write(b"MEM:READ? 1,20000084,4\n")
            assert port.readline() == b"000001F4\n"


def test_example_config_runs():
    cfg = load_config(EXAMPLE)
    assert sorted(cfg.device.mcus) == [0, 1]
    cfg.transport.tcp = "127.0.0.1:0"
    cfg.transport.udp = None
    with Simulator(cfg.device, cfg.transport) as s:
        sock, f = tcp_client(s)
        with sock:
            first = int.from_bytes(bytes.fromhex(ask(f, "MEM:READ? 0,20000010,4")), "little")
            time.sleep(0.02)
            second = int.from_bytes(bytes.fromhex(ask(f, "MEM:READ? 0,20000010,4")), "little")
            assert second > first  # tick_count is a live counter
            assert ask(f, "MEM:READ? 0,48000000,4") == "00000000"  # GPIOA range
