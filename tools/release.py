"""Release helper, used by the release workflow and build_release.ps1 (INS-7, INS-13, INS-14).

    python tools/release.py prepare 1.1.0      # set the version, close Unreleased in CHANGELOG.md
    python tools/release.py check v1.1.0       # the tag must match rdt/_version.py
    python tools/release.py notes 1.1.0        # print that version's release notes
    python tools/release.py version            # print the version in rdt/_version.py
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "rdt" / "_version.py"
CHANGELOG = ROOT / "CHANGELOG.md"
UNRELEASED = "## Unreleased"
SECTIONS = ("### New features", "### Bug fixes")
EMPTY_UNRELEASED = UNRELEASED + "\n\n" + "\n\n".join(SECTIONS) + "\n"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


class ReleaseError(Exception):
    pass


def check_version(version: str) -> str:
    version = version.strip().lstrip("v")
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError(f"{version!r} is not a version like 1.2.0")
    return version


def current_version(path: Path = VERSION_FILE) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    if not m:
        raise ReleaseError(f"no __version__ in {path}")
    return m.group(1)


def set_version(version: str, path: Path = VERSION_FILE) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', text), encoding="utf-8")


def _split(text: str) -> list[tuple[str, str]]:
    """The file as (heading, body) for each "## " section; the part before the first is ("", intro)."""
    parts = re.split(r"(?m)^(## .*)$", text)
    out = [("", parts[0])]
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1]))
    return out


def _entries(body: str) -> bool:
    return any(line.lstrip().startswith(("- ", "* ")) for line in body.splitlines())


def close_changelog(text: str, version: str, day: date) -> str:
    """Rename Unreleased to "version (date)", drop its empty subsections, and start a new empty
    Unreleased above it."""
    sections = _split(text)
    for i, (head, body) in enumerate(sections):
        if head == UNRELEASED:
            break
    else:
        raise ReleaseError(f"CHANGELOG.md has no '{UNRELEASED}' section")
    if any(h.startswith(f"## {version} ") or h == f"## {version}" for h, _ in sections):
        raise ReleaseError(f"CHANGELOG.md already has a {version} section")
    if not _entries(body):
        raise ReleaseError("Unreleased has no new features or bug fixes to release")
    kept = []
    for sub in re.split(r"(?m)^(?=### )", body.strip("\n")):
        if sub.strip() and (not sub.startswith("### ") or _entries(sub)):
            kept.append(sub.strip("\n"))
    sections[i] = (f"## {version} ({day.isoformat()})", "\n\n" + "\n\n".join(kept) + "\n\n")
    sections.insert(i, (UNRELEASED, "\n\n" + "\n\n".join(SECTIONS) + "\n\n"))
    out = sections[0][1] + "".join(h + b for h, b in sections[1:])
    return out.rstrip("\n") + "\n"


def notes(text: str, version: str) -> str:
    """The body of a version's section, for the GitHub release."""
    for head, body in _split(text):
        if head == f"## {version}" or head.startswith(f"## {version} "):
            return body.strip("\n") + "\n"
    raise ReleaseError(f"CHANGELOG.md has no {version} section")


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["prepare", version]:
                version = check_version(version)
                CHANGELOG.write_text(close_changelog(CHANGELOG.read_text(encoding="utf-8"), version, date.today()),
                                     encoding="utf-8")
                set_version(version)
                print(f"RDT {version}: version set, CHANGELOG.md closed")
            case ["check", tag]:
                version, have = check_version(tag), current_version()
                if version != have:
                    raise ReleaseError(f"tag {tag} does not match rdt/_version.py ({have}); "
                                       f"run 'python tools/release.py prepare {version}' first")
                print(version)
            case ["notes", version]:
                sys.stdout.write(notes(CHANGELOG.read_text(encoding="utf-8"), check_version(version)))
            case ["version"]:
                print(current_version())
            case _:
                print(__doc__.strip())
                return 2
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
