"""Files that ship with RDT: the user manual, the release notes, the licence and the third-party
notices (INS-12, INS-13).

From a source checkout they are in the repository (README beside docs/); in the installed app
PyInstaller puts them in a ``docs`` folder next to the code. ``doc_path`` finds either.
"""

from __future__ import annotations

import sys
from pathlib import Path

# name: where it is in the repository (the installed app keeps the same names in its docs folder)
DOCS = {
    "manual": "docs/USER_MANUAL.md",
    "changelog": "CHANGELOG.md",
    "license": "LICENSE",
    "notices": "THIRD_PARTY_NOTICES.md",
}


def bundle_root() -> Path:
    """The folder the installed app's files are in, or the repository for a source checkout."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def doc_path(name: str) -> Path:
    rel = DOCS[name]
    if getattr(sys, "frozen", False):
        return bundle_root() / "docs" / Path(rel).name
    return bundle_root() / rel


def read_doc(name: str) -> str | None:
    """The file's text, or None when it isn't there (an installed pip package has no docs)."""
    try:
        return doc_path(name).read_text(encoding="utf-8")
    except OSError:
        return None
