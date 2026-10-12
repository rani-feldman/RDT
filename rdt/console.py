"""SCPI console helpers shared by the GUI console and ``rdt console``: the command history kept
across launches (CON-1), the autocomplete suggestions (CON-2), the session log (CON-5) and hex/text
conversion (CON-6). No Qt here."""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .profile import Profile

log = logging.getLogger(__name__)

HISTORY_LIMIT = 500

# IEEE 488.2 common commands every SCPI instrument knows, offered by autocomplete.
COMMON_COMMANDS = ("*IDN?", "*RST", "*CLS", "*OPC?", "*ESR?", "*STB?", "*TST?", "*WAI", "SYSTem:ERRor?")


def history_path() -> Path:
    """Per-user history file: %LOCALAPPDATA%\\rdt\\console_history.txt on Windows."""
    if os.environ.get("RDT_HISTORY_FILE"):
        return Path(os.environ["RDT_HISTORY_FILE"])
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "rdt"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "rdt"
    return base / "console_history.txt"


class CommandHistory:
    """Commands sent by hand, oldest first, at most ``limit``; sending one again moves it to the end.

    Every change re-reads the file before writing it, so a GUI and a CLI console open at the same
    time both keep their commands. A file that can't be written leaves the history in memory only.
    """

    def __init__(self, path: Path | None = None, limit: int = HISTORY_LIMIT):
        self.path = path if path is not None else history_path()
        self.limit = limit
        self.items: list[str] = self._load()
        self._pos: int | None = None
        self._draft = ""

    # --- file --------------------------------------------------------------------------------

    def _load(self) -> list[str]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        items: list[str] = []
        for line in lines:
            line = line.strip()
            if line:
                if line in items:
                    items.remove(line)
                items.append(line)
        return items[-self.limit:]

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("".join(c + "\n" for c in self.items), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("console history not saved to %s: %s", self.path, e)

    def _change(self, edit) -> None:
        self.items = self._load() if self.path.exists() else self.items
        edit()
        self.items = self.items[-self.limit:]
        self._save()
        self.reset_navigation()

    # --- changes -----------------------------------------------------------------------------

    def add(self, command: str) -> None:
        command = command.strip()
        if not command:
            return

        def edit():
            if command in self.items:
                self.items.remove(command)
            self.items.append(command)
        self._change(edit)

    def remove(self, command: str) -> bool:
        if command not in self.items:
            return False
        def edit():
            if command in self.items:
                self.items.remove(command)
        self._change(edit)
        return True

    def clear(self) -> None:
        def edit():
            self.items = []
        self._change(edit)

    # --- Up and Down -------------------------------------------------------------------------

    def reset_navigation(self) -> None:
        self._pos = None
        self._draft = ""

    def navigate(self, step: int, current: str) -> str:
        """The text for the input line after Up (-1) or Down (+1). Going past the newest entry gives
        back what was being typed before Up was first pressed."""
        if not self.items:
            return current
        if self._pos is None:
            self._pos, self._draft = len(self.items), current
        self._pos = max(0, min(len(self.items), self._pos + step))
        if self._pos == len(self.items):
            draft = self._draft
            self.reset_navigation()
            return draft
        return self.items[self._pos]


def profile_commands(profile: Profile | None) -> list[str]:
    """The commands the SCPI profile defines, as they would be typed: a template's text up to its
    first field (``MEMory:READ? ``), and the error, size and probe queries in full."""
    if profile is None:
        return []
    out = []
    for template in (profile.read, profile.write):
        head = template.split("{", 1)[0]
        if head.strip():
            out.append(head)
    out += [q for q in (profile.error_query, profile.size_query, profile.probe) if q]
    return out


def completions(text: str, history: list[str], profile: Profile | None = None,
                extra: tuple[str, ...] | list[str] = (), limit: int = 20) -> list[str]:
    """Suggestions for what is typed so far, without duplicates (case-insensitive).

    Commands that start with ``text`` come first, then those that only contain it. Within each
    group, history comes first, newest first, then the profile's commands, ``extra``, and the
    common IEEE 488.2 commands.
    """
    needle = text.strip().casefold()
    pool: list[str] = []
    seen: set[str] = set()
    for c in [*reversed(history), *profile_commands(profile), *extra, *COMMON_COMMANDS]:
        key = c.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            pool.append(c)
    starts = [c for c in pool if c.casefold().startswith(needle)]
    contains = [c for c in pool if needle and needle in c.casefold() and c not in starts]
    return (starts + contains)[:limit]


# --- session log (CON-5) ---------------------------------------------------------------------

LOG_MARKS = {"tx": ">", "rx": "<", "error": "!", "note": "#"}


class SessionLog:
    """Appends every console line to a text file, with its date and time and a mark for its kind:
    ``>`` sent, ``<`` reply, ``!`` error, ``#`` note. Each line is flushed at once, so the file is
    complete even if RDT is killed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.file = open(self.path, "a", encoding="utf-8", newline="\n")
        self.write("note", "log started")

    def write(self, kind: str, text: str, when: datetime | None = None) -> None:
        if self.file is None:
            return
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        mark = LOG_MARKS.get(kind, "#")
        for line in text.split("\n") or [""]:
            self.file.write(f"{stamp} {mark} {line}\n")
        self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            self.write("note", "log stopped")
            self.file.close()
            self.file = None


# --- hex and text (CON-6) --------------------------------------------------------------------

def text_to_hex(text: str) -> str:
    """The UTF-8 bytes of ``text`` as spaced hex: ``Hi`` gives ``48 69``."""
    return " ".join(f"{b:02X}" for b in text.encode("utf-8"))


def hex_to_text(text: str) -> str:
    """Hex bytes back to text. Takes ``48656C6C6F``, ``48 65 6C``, ``0x48,0x65`` or ``48:65``.
    Printable ASCII is shown as is, tab and newline as themselves, any other byte as ``\\xNN``."""
    digits = text.replace(" ", " ")
    for sep in (",", ":", ";", "-", "\n", "\r", "\t"):
        digits = digits.replace(sep, " ")
    digits = "".join(w[2:] if w.lower().startswith("0x") else w for w in digits.split())
    if not digits:
        raise ValueError("no hex digits selected")
    if any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise ValueError("the selection is not hex: use 0-9 and A-F, with spaces, commas or 0x")
    if len(digits) % 2:
        raise ValueError(f"an odd number of hex digits ({len(digits)})")
    data = bytes.fromhex(digits)
    return "".join(chr(b) if 32 <= b < 127 or b in (9, 10) else f"\\x{b:02X}" for b in data)


# --- saved commands (CON-7) ------------------------------------------------------------------

COMMAND_COLUMNS = ("command", "name", "group", "description")


@dataclass
class SavedCommand:
    """One known command of the device, as imported from a list."""

    command: str
    name: str = ""
    group: str = ""
    description: str = ""


def read_commands(path: str | Path) -> list[SavedCommand]:
    """Read a command list: a CSV file (``.csv``) with the columns command, name, group and
    description (a header row names them in any order; without one they are taken in that
    order), or any other text file with one command per line. Blank lines and lines starting
    with ``#`` are skipped. Raises ValueError for a CSV file with no command column."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() != ".csv":
        return [SavedCommand(line.strip()) for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if "command" in header:
        index = {col: header.index(col) for col in COMMAND_COLUMNS if col in header}
        if "name" not in index and "label" in header:
            index["name"] = header.index("label")
        rows = rows[1:]
    elif any(h in COMMAND_COLUMNS for h in header):
        raise ValueError(f"{path.name}: the header has no 'command' column")
    else:
        index = {col: i for i, col in enumerate(COMMAND_COLUMNS)}
    out = []
    for r in rows:
        cell = {col: (r[i].strip() if i < len(r) else "") for col, i in index.items()}
        if cell.get("command") and not cell["command"].startswith("#"):
            out.append(SavedCommand(**cell))
    return out


def write_commands(path: str | Path, commands: list[SavedCommand]) -> None:
    """Write a command list as CSV with a header row, UTF-8 with a BOM so Excel opens it."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COMMAND_COLUMNS)
        for c in commands:
            w.writerow([c.command, c.name, c.group, c.description])


def merge_commands(current: list[SavedCommand], new: list[SavedCommand],
                   replace: bool = False) -> tuple[list[SavedCommand], int, int]:
    """Append ``new`` to ``current`` (or replace it), skipping commands already in the list,
    compared without case. Returns the list, how many were added and how many skipped."""
    out = [] if replace else list(current)
    seen = {c.command.casefold() for c in out}
    added = 0
    for c in new:
        key = c.command.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        added += 1
    return out, added, len(new) - added
