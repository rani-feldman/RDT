"""Console helpers: the saved command history (CON-1) and autocomplete (CON-2)."""

from rdt.console import COMMON_COMMANDS, CommandHistory, completions, history_path, profile_commands
from rdt.profile import Profile


def test_history_is_saved_and_shared(tmp_path):
    path = tmp_path / "h.txt"
    a = CommandHistory(path)
    for c in ("*IDN?", "MEM:MAX?", "*IDN?", "  ", "SYST:ERR?"):
        a.add(c)
    assert a.items == ["MEM:MAX?", "*IDN?", "SYST:ERR?"]     # sent again moves to the end
    assert path.read_text(encoding="utf-8").splitlines() == a.items
    # a second console (the CLI while the GUI is open) sees it, and both keep their commands
    b = CommandHistory(path)
    assert b.items == a.items
    b.add("*RST")
    a.add("*CLS")
    assert CommandHistory(path).items == ["MEM:MAX?", "*IDN?", "SYST:ERR?", "*RST", "*CLS"]
    assert a.remove("*IDN?") and not a.remove("nope")
    assert CommandHistory(path).items == ["MEM:MAX?", "SYST:ERR?", "*RST", "*CLS"]
    a.clear()
    assert CommandHistory(path).items == []


def test_history_limit_and_unwritable_file(tmp_path):
    h = CommandHistory(tmp_path / "h.txt", limit=3)
    for i in range(5):
        h.add(f"C{i}")
    assert h.items == ["C2", "C3", "C4"]
    blocked = tmp_path / "file"
    blocked.write_text("")
    h = CommandHistory(blocked / "h.txt")     # its folder is a file: kept in memory only
    h.add("*IDN?")
    assert h.items == ["*IDN?"]


def test_history_navigation_keeps_the_draft(tmp_path):
    h = CommandHistory(tmp_path / "h.txt")
    assert h.navigate(-1, "typing") == "typing"
    h.add("A")
    h.add("B")
    assert h.navigate(-1, "typing") == "B"
    assert h.navigate(-1, "B") == "A"
    assert h.navigate(-1, "A") == "A"           # stops at the oldest
    assert h.navigate(1, "A") == "B"
    assert h.navigate(1, "B") == "typing"       # past the newest: back to the draft


def test_history_path(monkeypatch, tmp_path):
    monkeypatch.setenv("RDT_HISTORY_FILE", str(tmp_path / "x.txt"))
    assert history_path() == tmp_path / "x.txt"
    monkeypatch.delenv("RDT_HISTORY_FILE")
    assert history_path().name == "console_history.txt" and history_path().parent.name == "rdt"


def test_profile_commands():
    p = Profile()
    assert profile_commands(p) == ["MEMory:READ? ", "MEMory:WRITE "]
    assert profile_commands(None) == []


def test_completions():
    history = ["MEMory:READ? 0,20000000,4", "*IDN?", "CONF:MODE 2"]
    found = completions("", history, Profile())
    # history newest first, then the profile, then common commands; no duplicates
    assert found[:3] == ["CONF:MODE 2", "*IDN?", "MEMory:READ? 0,20000000,4"]
    assert found.count("*IDN?") == 1 and len(found) <= 20
    assert "SYSTem:ERRor?" in found and "*RST" in found
    # case-insensitive; prefix matches before matches inside the command
    assert completions("mem", history, Profile()) == ["MEMory:READ? 0,20000000,4", "MEMory:READ? ",
                                                      "MEMory:WRITE "]
    assert completions("err", [], Profile()) == ["SYSTem:ERRor?"]
    assert completions("*", [], None) == list(COMMON_COMMANDS[:-1])
    assert completions(".h", [], None, (".history", ".help")) == [".history", ".help"]
    assert completions("zzz", history, Profile()) == []


def test_session_log(tmp_path):
    from datetime import datetime
    from rdt.console import SessionLog
    path = tmp_path / "s.txt"
    log = SessionLog(path)
    when = datetime(2026, 10, 12, 6, 58, 53, 123456)
    log.write("tx", "*IDN?", when)
    log.write("rx", "line 1\nline 2", when)
    log.write("error", "(no reply)", when)
    log.close()
    log.close()                       # twice is harmless
    log.write("rx", "after close")    # ignored
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1:4] == ["2026-10-12 06:58:53.123 > *IDN?", "2026-10-12 06:58:53.123 < line 1",
                          "2026-10-12 06:58:53.123 < line 2"]
    assert lines[4] == "2026-10-12 06:58:53.123 ! (no reply)"
    assert lines[0].endswith("# log started") and lines[-1].endswith("# log stopped") and len(lines) == 6
    SessionLog(path).close()          # appends, never overwrites
    assert len(path.read_text(encoding="utf-8").splitlines()) == 8


def test_hex_and_text():
    import pytest
    from rdt.console import hex_to_text, text_to_hex
    assert text_to_hex("Hi!") == "48 69 21"
    assert text_to_hex("µ") == "C2 B5"
    for hex_text in ("48656C6C6F", "48 65 6c 6C 6F", "0x48,0x65,0x6C,0x6C,0x6F", "48:65:6C:6C:6F", "48 65\n6C 6C 6F"):
        assert hex_to_text(hex_text) == "Hello"
    assert hex_to_text("41 00 FF 0A 09") == "A\\x00\\xFF\n\t"
    for bad, msg in (("", "no hex"), ("486", "odd"), ("zz", "not hex")):
        with pytest.raises(ValueError, match=msg):
            hex_to_text(bad)


def test_read_and_merge_commands(tmp_path):
    import pytest
    from rdt.console import SavedCommand, merge_commands, read_commands, write_commands
    plain = tmp_path / "a.csv"
    plain.write_text("MEAS:VOLT?,Volts,Measure,Output voltage\nOUTP ON\n\n#x,y\n", encoding="utf-8-sig")
    assert read_commands(plain) == [SavedCommand("MEAS:VOLT?", "Volts", "Measure", "Output voltage"),
                                    SavedCommand("OUTP ON")]
    labelled = tmp_path / "b.csv"
    labelled.write_text("Label,Command\nIdentify,*IDN?\n", encoding="utf-8")
    assert read_commands(labelled) == [SavedCommand("*IDN?", "Identify")]
    out = tmp_path / "c.csv"
    write_commands(out, read_commands(plain))
    assert read_commands(out) == read_commands(plain)
    bad = tmp_path / "d.csv"
    bad.write_text("name,group\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'command' column"):
        read_commands(bad)
    semi = tmp_path / "e.csv"   # Excel in some regions: semicolons, and the Windows code page
    semi.write_bytes("command;name;group\n*RST;*CLS reset;Basic\nMEAS:TEMP?;Temp \xb0C;Measure\n".encode("cp1252"))
    assert read_commands(semi) == [SavedCommand("*RST", "*CLS reset", "Basic"),
                                   SavedCommand("MEAS:TEMP?", "Temp \xb0C", "Measure")]
    tabs = tmp_path / "f.csv"
    tabs.write_text("*IDN?\tIdentify\nSYST:ERR?\tErrors\n", encoding="utf-8")
    assert [(c.command, c.name) for c in read_commands(tabs)] == [("*IDN?", "Identify"), ("SYST:ERR?", "Errors")]
    commas = tmp_path / "g.csv"   # a semicolon inside a command doesn't change the separator
    commas.write_text("*RST;*CLS,Reset all\n", encoding="utf-8")
    assert read_commands(commas) == [SavedCommand("*RST;*CLS", "Reset all")]
    cur = [SavedCommand("*IDN?")]
    merged, added, skipped = merge_commands(cur, [SavedCommand("*idn?"), SavedCommand("A"), SavedCommand("A")])
    assert ([c.command for c in merged], added, skipped) == (["*IDN?", "A"], 1, 2)
    assert merge_commands(cur, [SavedCommand("B")], replace=True)[0] == [SavedCommand("B")]
