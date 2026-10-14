"""Memory map of one MCU: where RDT may read and write.

RDT refuses any access outside the ELF's allocated sections (RAM and flash) and the extra
ranges listed in the profile or the target file. Sections without the write flag, such as
``.text`` and ``.rodata``, are read-only, also where a listed memory range covers them.
A read may span adjacent ELF sections and memory ranges. A peripheral range is accessed on its
own: an access that touches one must stay inside it, and it is never merged with anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import AccessRefused, ConfigError
from .profile import Range, align_span


@dataclass
class Area:
    name: str
    start: int
    size: int
    writable: bool
    peripheral: bool     # a listed peripheral range: never merged, accessed on its own
    memory: bool = False  # a listed memory range (RAM or flash), which may overlap ELF sections

    @property
    def kind(self) -> str:
        return "peripheral" if self.peripheral else "memory" if self.memory else "section"

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class ElfInfo:
    path: Path
    byte_order: str
    sections: list[Area]


def load_elf_sections(path: str | Path) -> ElfInfo:
    from elftools.common.exceptions import ELFError
    from elftools.elf.constants import SH_FLAGS
    from elftools.elf.elffile import ELFFile

    path = Path(path)
    try:
        with open(path, "rb") as f:
            elf = ELFFile(f)
            areas = []
            for sec in elf.iter_sections():
                if not sec["sh_flags"] & SH_FLAGS.SHF_ALLOC or sec["sh_size"] == 0:
                    continue
                writable = bool(sec["sh_flags"] & SH_FLAGS.SHF_WRITE)
                areas.append(Area(sec.name, sec["sh_addr"], sec["sh_size"], writable, False))
            order = "little" if elf.little_endian else "big"
    except OSError as e:
        raise ConfigError(f"cannot read ELF {path}: {e.strerror}") from None
    except ELFError as e:
        raise ConfigError(f"{path} is not a valid ELF file: {e}") from None
    return ElfInfo(path, order, sorted(areas, key=lambda a: a.start))


class MemoryMap:
    def __init__(self, sections: list[Area] | None = None, ranges: list[Range] | None = None):
        areas = list(sections or [])
        areas += [Area(r.name, r.start, r.size, r.writable, r.kind == "peripheral", r.kind == "memory")
                  for r in ranges or []]
        areas.sort(key=lambda a: (a.start, -a.size))
        for p in (a for a in areas if a.peripheral):
            for a in areas:
                if a is not p and a.start < p.end and p.start < a.end:
                    raise ConfigError(f"peripheral range {p.name} overlaps {a.name}")
        self.areas = areas

    def check(self, addr: int, size: int, write: bool = False) -> list[Area]:
        """Return the areas the access covers, or raise ``AccessRefused``."""
        if size < 1:
            raise AccessRefused("size must be at least 1")
        if addr < 0:
            raise AccessRefused("address must not be negative")
        end = addr + size
        what = f"0x{addr:08X}..0x{end - 1:08X}"
        covered: list[Area] = []
        pos = addr
        for a in self.areas:
            if a.end <= pos:
                continue
            if a.start > pos:
                break
            covered.append(a)
            pos = max(pos, a.end)
            if pos >= end:
                break
        if pos < end:
            raise AccessRefused(f"{what} is outside the MCU's known memory (0x{pos:08X} is not in the ELF "
                                f"or a listed range)")
        touched = [a for a in self.areas if a.start < end and addr < a.end]   # overlapping ones too
        if any(a.peripheral for a in covered) and len(covered) > 1:
            raise AccessRefused(f"{what} crosses the edge of range {next(a.name for a in covered if a.peripheral)}; "
                                f"listed ranges are accessed on their own")
        if write:
            for a in touched:   # a read-only section stays read-only inside a writable memory range
                if not a.writable:
                    raise AccessRefused(f"{what} is in read-only {a.name}")
        return covered

    def check_aligned(self, addr: int, size: int, align: int, write: bool = False) -> list[Area]:
        """Check an access that the device makes in whole aligned words.

        The bytes asked for must pass ``check``. The extra bytes the aligned words add around
        them are allowed in unlisted memory or another section, since the firmware reads whole
        words anyway, but not in a peripheral range the access is not already in (reading a
        register can clear a flag), nor in a read-only area when the words are written back.
        """
        covered = self.check(addr, size, write)
        start, end = align_span(addr, size, align)
        if (start, end) == (addr, addr + size):
            return covered
        what = f"the {align}-byte aligned access 0x{start:08X}..0x{end - 1:08X}"
        for p in (a for a in covered if a.peripheral):
            if start < p.start or end > p.end:
                raise AccessRefused(f"{what} crosses the edge of range {p.name}")
        for lo, hi in ((start, addr), (addr + size, end)):
            for a in self.areas:
                if a.start < hi and lo < a.end:
                    if a.peripheral and a not in covered:
                        raise AccessRefused(f"{what} reaches into range {a.name}")
                    if write and not a.writable:
                        raise AccessRefused(f"{what} reaches into read-only {a.name}")
        return covered

    def find(self, addr: int) -> Area | None:
        for a in self.areas:
            if a.start <= addr < a.end:
                return a
        return None


BUILD_ID_SECTION = ".note.gnu.build-id"


def elf_bytes(path: str | Path, addr: int, size: int) -> bytes | None:
    """The bytes the ELF file itself holds at a run address, or None when they are not in the
    file (``.bss``, or outside every loaded section)."""
    from elftools.common.exceptions import ELFError
    from elftools.elf.constants import SH_FLAGS
    from elftools.elf.elffile import ELFFile

    try:
        with open(path, "rb") as f:
            for sec in ELFFile(f).iter_sections():
                if not sec["sh_flags"] & SH_FLAGS.SHF_ALLOC or sec["sh_type"] == "SHT_NOBITS":
                    continue
                start = sec["sh_addr"]
                if start <= addr and addr + size <= start + sec["sh_size"]:
                    return bytes(sec.data()[addr - start: addr - start + size])
    except OSError as e:
        raise ConfigError(f"cannot read ELF {path}: {e.strerror}") from None
    except ELFError as e:
        raise ConfigError(f"{path} is not a valid ELF file: {e}") from None
    return None


FLASH_SAMPLE_BLOCK = 64     # bytes per sampled block
FLASH_SAMPLE_SPREAD = 32    # blocks spread evenly over all read-only sections, besides each one's ends


def _runs_where_flashed(elf, sec) -> bool:
    """False for a section that runs from another address than it is flashed at, such as code
    a Keil scatter file places in ITCM or RAM: the startup code copies it there from flash, so
    its ELF address holds nothing until then and may be changed after.

    Where it is flashed comes from the load segment holding its bytes in the file: the segment's
    physical address plus the section's place in it. armlink may put such a section's bytes in
    the flash segment with no segment of its own (ER_IROM2 at 0x00000000 inside the segment at
    0x08040000), so the section's file offset is what finds its segment, not its address."""
    loads = [seg for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"]
    off = sec["sh_offset"]
    for seg in loads:
        if seg["p_filesz"] and seg["p_offset"] <= off < seg["p_offset"] + seg["p_filesz"]:
            return seg["p_paddr"] + (off - seg["p_offset"]) == sec["sh_addr"]
    for seg in loads:   # no bytes in the file for it: judge by the segment it runs in
        if seg["p_vaddr"] <= sec["sh_addr"] < seg["p_vaddr"] + max(seg["p_memsz"], 1):
            return seg["p_paddr"] == seg["p_vaddr"]
    return True


def flash_samples(path: str | Path, block: int = FLASH_SAMPLE_BLOCK,
                  spread: int = FLASH_SAMPLE_SPREAD) -> list[tuple[int, bytes]]:
    """Blocks of the firmware image to compare with the device: the start and end of every
    read-only section the ELF holds bytes for (code, constants, the vector table) and that runs
    from where it is flashed (not code copied to ITCM or RAM at startup), plus
    ``spread`` blocks spread evenly over all of them. Returns (address, bytes in the ELF) in
    address order, overlapping blocks merged, each trimmed to whole 4-byte words."""
    from elftools.common.exceptions import ELFError
    from elftools.elf.constants import SH_FLAGS
    from elftools.elf.elffile import ELFFile

    try:
        with open(path, "rb") as f:
            elf = ELFFile(f)
            secs = [(sec["sh_addr"], sec.data()) for sec in elf.iter_sections()
                    if sec["sh_flags"] & SH_FLAGS.SHF_ALLOC and not sec["sh_flags"] & SH_FLAGS.SHF_WRITE
                    and sec["sh_type"] != "SHT_NOBITS" and sec["sh_size"] and _runs_where_flashed(elf, sec)]
    except OSError as e:
        raise ConfigError(f"cannot read ELF {path}: {e.strerror}") from None
    except ELFError as e:
        raise ConfigError(f"{path} is not a valid ELF file: {e}") from None
    total = sum(len(d) for _, d in secs)
    if not total:
        return []
    picks = {i: [0, max(0, len(d) - block)] for i, (_, d) in enumerate(secs)}
    step = total / spread
    for k in range(spread):
        at = int(step * k + step / 2)                       # the middle of each stretch
        for i, (_, d) in enumerate(secs):
            if at < len(d):
                picks[i].append(max(0, min(at - at % 4, len(d) - block)))
                break
            at -= len(d)
    out = []
    for i, (start, d) in enumerate(secs):
        spans = []
        for off in sorted(set(picks[i])):
            end = min(off + block, len(d))
            if spans and off <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([off, end])
        for lo, hi in spans:   # whole aligned words inside the section, so no read reaches a gap after it
            lo += -(start + lo) % 4
            hi -= (start + hi) % 4
            if hi > lo:
                out.append((start + lo, bytes(d[lo:hi])))
    return sorted(out)


def build_id_note(path: str | Path) -> tuple[int, int] | None:
    """Address and size of the GNU build-ID note, when the linker placed it in the firmware's
    memory (an allocated section); None otherwise."""
    from elftools.elf.constants import SH_FLAGS
    from elftools.elf.elffile import ELFFile

    with open(path, "rb") as f:
        sec = ELFFile(f).get_section_by_name(BUILD_ID_SECTION)
        if sec is None or not sec["sh_flags"] & SH_FLAGS.SHF_ALLOC or not sec["sh_size"]:
            return None
        return sec["sh_addr"], sec["sh_size"]
