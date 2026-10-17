"""Is there a newer RDT? (INS-9)

RDT asks GitHub for the repository's latest published release, at most once a day, and the GUI
says when it is newer than the running version. Drafts are not counted: GitHub only shows a
release once rani presses Publish. The check needs no sign-in because the repository is public.

    latest = fetch_latest()            # Release("1.1.0", "https://github.com/...") or None
    if latest and is_newer(latest.version, __version__): ...

Any failure (no network, a proxy, GitHub down) gives None and the GUI says nothing.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date

from ._version import __version__

REPO = "rani-feldman/rdt"
RELEASES_URL = f"https://github.com/{REPO}/releases"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT_S = 5


@dataclass(frozen=True)
class Release:
    version: str     # "1.1.0", without the tag's v
    url: str         # the release's page on GitHub


def parse_version(text: str) -> tuple[int, ...] | None:
    """(1, 1, 0) from "1.1.0" or "v1.1.0"; None for anything that isn't numbers and dots."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def due(last_check: str | None, today: date | None = None) -> bool:
    """Whether to check today: not yet checked today (last_check is an ISO date, as saved in ui.toml)."""
    today = today or date.today()
    try:
        return date.fromisoformat(last_check or "") < today
    except ValueError:
        return True


def fetch_latest(timeout: float = TIMEOUT_S) -> Release | None:
    """The latest published release on GitHub, or None when it can't be read."""
    req = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"RDT/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return release_from(data)
    except Exception:     # network, HTTP, JSON: never worth bothering the user about
        return None


def release_from(data: dict) -> Release | None:
    tag = str(data.get("tag_name", ""))
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        return None
    return Release(tag.lstrip("v"), str(data.get("html_url") or RELEASES_URL))
