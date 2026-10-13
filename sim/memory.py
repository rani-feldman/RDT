"""Simulated MCU memory: regions loaded from an ELF or declared in the config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from elftools.elf.constants import SH_FLAGS
from elftools.elf.elffile import ELFFile


class MemoryFault(Exception):
    """An access touched an address with no memory behind it, or wrote to read-only memory."""


@dataclass
class Region:
    name: str
    start: int
    data: bytearray
    writable: bool
    backdrop: bool = False   # a whole RAM or flash bank from the config; ELF sections are laid over it
    initial: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.initial = bytes(self.data)

    @property
    def end(self) -> int:
        return self.start + len(self.data)


class Memory:
    def __init__(self) -> None:
        self.regions: list[Region] = []

    def add(self, region: Region) -> None:
        """Add a region. It may overlap backdrop regions (memory banks), which it replaces where
        they overlap; overlapping any other region is an error."""
        if not region.data:
            return
        for r in self.regions:
            if region.start < r.end and r.start < region.end and not r.backdrop:
                raise ValueError(f"region {region.name} overlaps {r.name}")
        kept = []
        for r in self.regions:
            if not (region.start < r.end and r.start < region.end):
                kept.append(r)
                continue
            if r.start < region.start:   # the part of the bank below the new region
                kept.append(Region(r.name, r.start, r.data[:region.start - r.start], r.writable, True))
            if region.end < r.end:       # and the part above it
                kept.append(Region(r.name, region.end, r.data[region.end - r.start:], r.writable, True))
        kept.append(region)
        self.regions = sorted(kept, key=lambda r: r.start)

    def load_elf(self, path: str | Path) -> str:
        """Load every allocated section at its run address; returns the ELF's byte order.

        ``.data`` gets its initial values and ``.bss`` is zero, as after the firmware's startup code.
        A section inside a memory bank declared in the config replaces that part of the bank.
        """
        with open(path, "rb") as f:
            elf = ELFFile(f)
            for sec in elf.iter_sections():
                if not sec["sh_flags"] & SH_FLAGS.SHF_ALLOC or sec["sh_size"] == 0:
                    continue
                if sec["sh_type"] == "SHT_NOBITS":
                    data = bytearray(sec["sh_size"])
                else:
                    data = bytearray(sec.data())
                writable = bool(sec["sh_flags"] & SH_FLAGS.SHF_WRITE)
                self.add(Region(sec.name, sec["sh_addr"], data, writable))
            return "little" if elf.little_endian else "big"

    def reset(self) -> None:
        for r in self.regions:
            r.data[:] = r.initial

    def _spans(self, addr: int, size: int):
        """Yield (region, offset, length) pieces covering [addr, addr+size), across adjacent regions."""
        if size <= 0:
            return
        end = addr + size
        for r in self.regions:
            if r.end <= addr:
                continue
            if r.start > addr:
                break
            n = min(end, r.end) - addr
            yield r, addr - r.start, n
            addr += n
            if addr == end:
                return
        raise MemoryFault(f"address 0x{addr:X} is not mapped")

    def read(self, addr: int, size: int) -> bytes:
        return b"".join(bytes(r.data[off:off + n]) for r, off, n in self._spans(addr, size))

    def write(self, addr: int, data: bytes, *, force: bool = False) -> None:
        """Write ``data``; ``force`` also writes read-only memory, as the firmware itself may."""
        spans = list(self._spans(addr, len(data)))
        for r, off, _ in spans:
            if not r.writable and not force:
                raise MemoryFault(f"address 0x{r.start + off:X} ({r.name}) is read-only")
        pos = 0
        for r, off, n in spans:
            r.data[off:off + n] = data[pos:pos + n]
            pos += n
