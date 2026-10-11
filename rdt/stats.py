"""Statistics per watched value: minimum, maximum, mean and sample count, with a reset.

Values are the shown ones, after scaling. A bool counts as 0 or 1 and an enum as its number;
anything that is not a number (a string, a struct) is not counted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def numeric(value) -> float | None:
    """A shown value as a number for statistics, the chart and the CSV log, or None."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return None if math.isnan(v) else v
    return None


@dataclass
class Stats:
    n: int = 0
    min: float = math.inf
    max: float = -math.inf
    total: float = 0.0

    def add(self, v: float) -> None:
        self.n += 1
        self.total += v
        if v < self.min:
            self.min = v
        if v > self.max:
            self.max = v

    @property
    def mean(self) -> float | None:
        return self.total / self.n if self.n else None


class StatsBook:
    """Statistics for many values, by path (``main:motor.speed``, ``main:gains[2]``)."""

    def __init__(self):
        self.stats: dict[str, Stats] = {}

    def add(self, path: str, value) -> Stats | None:
        v = numeric(value)
        if v is None or math.isinf(v):
            return self.stats.get(path)
        st = self.stats.setdefault(path, Stats())
        st.add(v)
        return st

    def get(self, path: str) -> Stats | None:
        return self.stats.get(path)

    def reset(self, prefix: str | None = None) -> None:
        """Forget everything, or only ``prefix`` and its fields and cells."""
        if prefix is None:
            self.stats.clear()
            return
        for path in [p for p in self.stats if p == prefix or p.startswith(prefix) and p[len(prefix)] in ".[-"]:
            del self.stats[path]
