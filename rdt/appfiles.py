"""Files that ship with RDT: the user manual, the release notes, the licence and the third-party
notices (INS-12, INS-13), the walkthrough video and the presentation (INS-16, INS-17), and the
default workspace in examples (WSP-9).

From a source checkout they are in the repository (README beside docs/); in the installed app
PyInstaller puts them in a ``docs`` folder next to the code. ``doc_path`` finds either.
"""

from __future__ import annotations

import shutil
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


# The walkthrough video and the presentation (INS-16). The installer and the zip carry the
# presentation in docs/media (INS-17, rani 2026-10-28); GitHub is the fallback when it is missing.
# The video is not shipped and always opens from the repository (rani, 2026-10-29).
MEDIA = {
    "video": "docs/media/rdt-walkthrough.mp4",
    "presentation": "docs/media/rdt-presentation.pdf",
}
ONLINE_ONLY = {"video"}
GITHUB_FILES = "https://github.com/rani-feldman/rdt/blob/main/"


def media_url(name: str) -> str:
    """Where Help opens the video or the presentation: the video's page in the repository; the
    presentation that ships with RDT, else its page on GitHub."""
    rel = MEDIA[name]
    local = bundle_root() / rel
    if name not in ONLINE_ONLY and local.is_file():
        return local.as_uri()
    return GITHUB_FILES + rel


def read_doc(name: str) -> str | None:
    """The file's text, or None when it isn't there (an installed pip package has no docs)."""
    try:
        return doc_path(name).read_text(encoding="utf-8")
    except OSError:
        return None


# The default workspace: the bench files that ship in examples/ (rani, 2026-10-29). The GUI copies
# them into the workspace folder the first time it starts with nothing else to open, so the user
# edits and saves their own copy, and a new install or an uninstall never touches it (WSP-9).
DEFAULT_WORKSPACE = ("bench.rdtw", "bench.profile.toml", "bench.memory.toml", "bench.watch.toml",
                     "bench.commands.toml")


def examples_dir() -> Path:
    """The examples folder: beside the exes in the installed app (build_release.ps1 copies it
    there), in the repository for a source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "examples"
    return bundle_root() / "examples"


def install_default_workspace(folder: Path) -> Path | None:
    """Copy the default workspace's files from examples into folder and return its .rdtw. A file
    already in folder is kept, so the user's own bench.rdtw is never overwritten. None when the
    examples are not there (an installed pip package has none) or the folder can't be written."""
    src = examples_dir()
    if not (src / DEFAULT_WORKSPACE[0]).is_file():
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in DEFAULT_WORKSPACE:
            if (src / name).is_file() and not (folder / name).exists():
                shutil.copyfile(src / name, folder / name)
    except OSError:
        return None
    return folder / DEFAULT_WORKSPACE[0]
