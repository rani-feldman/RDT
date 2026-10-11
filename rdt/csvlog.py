"""CSV log of watched values: one row per poll, ISO 8601 timestamps, shown (scaled) values.

    time,seconds,main:motor.speed [rpm],main:motor.mode,event
    2026-10-11T14:03:22.120+03:00,0.000,1500.5,1,
    2026-10-11T14:03:22.220+03:00,0.100,1502.25,,
    2026-10-11T14:03:23.950+03:00,1.830,,,link down

- ``time`` is the PC's clock with its UTC offset; ``seconds`` counts from the first row.
- A column holds the shown value: scaled, a bool as 0 or 1, an enum as its number. A ``char``
  array is written as its string.
- A cell is empty when that value was not read in that poll (it has its own interval, or it
  failed). The ``event`` column marks a lost link, its return, and ELF rebuilds.
- The file is UTF-8 with a byte order mark, so Excel shows units like ``°C`` correctly.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

from .errors import ConfigError
from .stats import numeric


def iso_time(when: float) -> str:
    return datetime.fromtimestamp(when).astimezone().isoformat(timespec="milliseconds")


def cell(value) -> str:
    """One value as CSV text: numbers as numbers, anything else as its text."""
    v = numeric(value)
    if v is not None:
        if isinstance(value, float) or not v.is_integer():
            return f"{v:.9g}"
        return str(int(v))
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("latin-1")
    return "" if value is None else str(value)


class CsvLog:
    def __init__(self, path: str | Path, columns: list[str], units: dict[str, str] | None = None):
        """``columns`` are the paths logged, in order; ``units`` adds `` [unit]`` to a header."""
        self.path = Path(path)
        self.columns = list(columns)
        self.rows = 0
        self._index = {c: i for i, c in enumerate(self.columns)}
        self._start: float | None = None
        try:
            self._file = open(self.path, "w", newline="", encoding="utf-8-sig")
        except OSError as e:
            raise ConfigError(f"cannot write {self.path}: {e.strerror}") from None
        self._csv = csv.writer(self._file)
        units = units or {}
        self._csv.writerow(["time", "seconds"] + [f"{c} [{units[c]}]" if units.get(c) else c for c in self.columns]
                           + ["event"])
        self._file.flush()

    def write(self, when: float, values: dict[str, object], event: str = "") -> None:
        """One row: ``values`` by path; paths that aren't columns are ignored."""
        cells = [""] * len(self.columns)
        for path, value in values.items():
            i = self._index.get(path)
            if i is not None:
                cells[i] = cell(value)
        if self._start is None:
            self._start = when
        self._csv.writerow([iso_time(when), f"{when - self._start:.3f}"] + cells + [event])
        self._file.flush()
        self.rows += 1

    def event(self, when: float, text: str) -> None:
        self.write(when, {}, text)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> CsvLog:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
