"""Is there a newer RDT? (INS-9)

RDT asks GitHub for the repository's latest published release, at most once a day, and the GUI
says when it is newer than the running version. It asks the plain web page
github.com/<repo>/releases/latest, not api.github.com: GitHub answers that page with a redirect to
/releases/tag/vX.Y.Z, and RDT reads the version from the redirect without following it. The web
page isn't counted against the API's limit of 60 requests an hour per IP address, which many PCs
behind one office IP could reach. Drafts and pre-releases are never "latest". The check needs no
sign-in because the repository is public.

When the release is newer, RDT also reads that tag's CHANGELOG.md from raw.githubusercontent.com
(not the API either) and keeps the sections of the versions newer than the running one, so the
GUI can show what changed.

    latest = fetch_latest()            # Release("1.1.0", "https://github.com/...") or None
    if latest and is_newer(latest.version, __version__): ...

Any failure (no network, a proxy, GitHub down) gives None and the GUI says nothing.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import date

from ._version import __version__

REPO = "rani-feldman/rdt"
RELEASES_URL = f"https://github.com/{REPO}/releases"
LATEST_URL = f"{RELEASES_URL}/latest"     # redirects to {RELEASES_URL}/tag/vX.Y.Z
CHANGELOG_URL = "https://raw.githubusercontent.com/" + REPO + "/{tag}/CHANGELOG.md"
TIMEOUT_S = 5


@dataclass(frozen=True)
class Release:
    version: str     # "1.1.0", without the tag's v
    url: str         # the release's page on GitHub
    notes: str = ""  # CHANGELOG.md sections newer than the running version, Markdown; "" if unknown


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


def fetch_latest(timeout: float = TIMEOUT_S, current: str = __version__) -> Release | None:
    """The latest published release on GitHub, or None when it can't be read. When it is newer than
    ``current`` it carries the release notes since ``current`` (empty when those can't be read)."""
    release = _latest_release(timeout)
    if release and is_newer(release.version, current):
        return replace(release, notes=changes_since(fetch_changelog(release.version, timeout), current))
    return release


def _latest_release(timeout: float) -> Release | None:
    req = urllib.request.Request(LATEST_URL, method="HEAD", headers={"User-Agent": f"RDT/{__version__}"})
    try:
        _no_redirect.open(req, timeout=timeout).close()
        return None       # no redirect: the repository has no published release
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            return None
        return release_from_location(e.headers.get("Location", ""))
    except Exception:     # network, proxy, timeout: never worth bothering the user about
        return None


def release_from_location(location: str) -> Release | None:
    """Release("1.2.0", url) from the redirect target ".../releases/tag/v1.2.0"; None otherwise."""
    m = re.search(r"/releases/tag/([^/?#]+)$", location.strip())
    if not m or parse_version(m.group(1)) is None:
        return None
    url = location if location.startswith("https://") else f"{RELEASES_URL}/tag/{m.group(1)}"
    return Release(m.group(1).lstrip("v"), url)


def fetch_changelog(version: str, timeout: float = TIMEOUT_S) -> str:
    """CHANGELOG.md as tagged v<version>, or "" when it can't be read."""
    req = urllib.request.Request(CHANGELOG_URL.format(tag=f"v{version}"),
                                 headers={"User-Agent": f"RDT/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(1_000_000).decode("utf-8", "replace")
    except Exception:
        return ""


def changes_since(changelog: str, current: str = __version__) -> str:
    """The "## X.Y.Z (date)" sections of CHANGELOG.md newer than ``current``, newest first, with
    empty headings (a "### Bug fixes" with no lines) left out. Unreleased is never included."""
    out: list[str] = []
    keep = False
    for line in changelog.splitlines():
        if line.startswith("## "):
            m = re.match(r"##\s+v?(\d+(?:\.\d+)*)\b", line)
            keep = bool(m) and is_newer(m.group(1), current)
        if keep:
            out.append(line)
    return _drop_empty_headings("\n".join(out)).strip()


def _drop_empty_headings(text: str) -> str:
    lines = text.splitlines()
    kept = []
    for i, line in enumerate(lines):
        if line.startswith("### "):
            nxt = next((n for n in lines[i + 1:] if n.strip()), "")
            if not nxt or nxt.startswith("#"):
                continue
        kept.append(line)
    return "\n".join(kept)


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow redirects: urllib then raises HTTPError carrying the Location header."""

    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect = urllib.request.build_opener(_KeepRedirect)
