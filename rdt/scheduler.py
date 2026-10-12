"""Poll scheduler: read many watched items in as few reads as possible.

Each item is resolved first; an item that goes through a pointer reads the pointer again. The
items of each MCU are sorted by address, and items close to each other are merged into one block
no larger than the device's ``max_response``, so one read brings them all from the same moment.

- Items in a listed range (a peripheral) are never merged: each is read on its own at its exact
  size, because reading a register can clear a flag or pop a FIFO.
- A block must lie in memory RDT may read; a gap that leaves the ELF's sections splits it.
- If a merged block fails for a reason other than a lost link or a silent MCU, its items are read
  again one by one, so one bad item can't take its neighbours down with it.

    samples = read_many(target, ["main:motor.speed", "main:tick_count", "motorctl:temperature"])

Every sample carries the PC's clock (``time.time()``) at the moment its reply arrived.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from .errors import LinkDown, McuSilent, RdtError
from .profile import align_span
from .symbols import Location
from .target import Target

MERGE_GAP = 32   # bytes: items further apart than this are read separately


@dataclass
class Sample:
    """One item's bytes from one poll, or why there are none."""

    expr: str
    loc: Location | None = None
    data: bytes | None = None
    time: float = 0.0               # time.time() when the reply arrived
    error: Exception | None = None


@dataclass
class Block:
    """One read: a run of memory on one MCU and the items it holds."""

    mcu: str
    addr: int
    size: int
    items: list[int] = field(default_factory=list)   # indices into the list of locations
    peripheral: bool = False

    @property
    def end(self) -> int:
        return self.addr + self.size


def plan(t: Target, locs: Sequence[Location | None], gap: int = MERGE_GAP) -> list[Block]:
    """Group locations into reads. ``None`` entries are skipped. A block holding several items is
    at most ``max_response`` bytes; one item larger than that is still one block (the session
    splits the read)."""
    by_mcu: dict[str, list[int]] = {}
    for i, loc in enumerate(locs):
        if loc is not None:
            by_mcu.setdefault(loc.mcu, []).append(i)
    blocks: list[Block] = []
    limit = t.max_response - t.max_response % t.align   # the largest aligned read one reply carries
    for mcu, idx in by_mcu.items():
        memory = t.mcu(mcu).memory
        idx.sort(key=lambda i: (locs[i].addr, -locs[i].size))
        cur: Block | None = None
        for i in idx:
            loc = locs[i]
            area = memory.find(loc.addr)
            peripheral = area is not None and area.peripheral
            if cur is not None and not peripheral and not cur.peripheral:
                end = max(cur.end, loc.addr + loc.size)
                a0, a1 = align_span(cur.addr, end - cur.addr, t.align)
                if loc.addr - cur.end <= gap and (a1 - a0 <= limit or end == cur.end) \
                        and _readable(memory, cur.addr, end - cur.addr):
                    cur.size = end - cur.addr
                    cur.items.append(i)
                    continue
            cur = Block(mcu, loc.addr, loc.size, [i], peripheral)
            blocks.append(cur)
    return blocks


def _readable(memory, addr: int, size: int) -> bool:
    try:
        covered = memory.check(addr, size)
    except RdtError:
        return False
    return not any(a.peripheral for a in covered)


def read_many(t: Target, items: Sequence[str | Location], gap: int = MERGE_GAP) -> list[Sample]:
    """Read every item, merging nearby ones; returns one sample per item, in the same order.
    Errors are kept per item, never raised."""
    samples = [Sample(it if isinstance(it, str) else it.path) for it in items]
    locs: list[Location | None] = []
    for s, it in zip(samples, items):
        try:
            loc = t.resolve(it)
            t._check(t.mcu(loc.mcu), loc.addr, loc.size, write=False)
        except RdtError as e:
            s.error, s.time, loc = e, time.time(), None
        else:
            s.loc = loc
        locs.append(loc)
    for block in plan(t, locs, gap):
        _read_block(t, block, locs, samples)
    return samples


def _read_block(t: Target, block: Block, locs: list[Location | None], samples: list[Sample]) -> None:
    try:
        data = t.read(block.mcu, block.addr, block.size)
    except (LinkDown, McuSilent) as e:
        for i in block.items:
            samples[i].error, samples[i].time = e, time.time()
        return
    except RdtError as e:
        if len(block.items) == 1:
            samples[block.items[0]].error, samples[block.items[0]].time = e, time.time()
            return
        for i in block.items:   # one at a time, so a bad item fails alone
            _read_block(t, Block(block.mcu, locs[i].addr, locs[i].size, [i]), locs, samples)
        return
    now = time.time()
    for i in block.items:
        off = locs[i].addr - block.addr
        samples[i].data = data[off:off + locs[i].size]
        samples[i].time = now
