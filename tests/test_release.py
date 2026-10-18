"""Versions, release notes and the update check (INS-7, INS-9, INS-11, INS-13, INS-14)."""

import importlib.util
import re
from datetime import date
from pathlib import Path

import pytest

import rdt
from rdt import appfiles, updates
from rdt.workspace import FORMAT, Workspace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release", ROOT / "tools" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

LOG = """# Release notes

Intro.

## Unreleased

### New features

- Feature A

### Bug fixes

## 1.0.0 (2026-10-17)

### New features

- First
"""


def test_one_version_everywhere():
    assert rdt.__version__ == release.current_version()
    assert updates.parse_version(rdt.__version__) is not None


def test_close_changelog_keeps_filled_sections_and_starts_a_new_unreleased():
    out = release.close_changelog(LOG, "1.1.0", date(2026, 11, 2))
    assert out.index("## Unreleased") < out.index("## 1.1.0 (2026-11-02)") < out.index("## 1.0.0")
    assert release.notes(out, "1.1.0") == "### New features\n\n- Feature A\n"   # the empty Bug fixes is dropped
    unreleased = out.split("## Unreleased")[1].split("## 1.1.0")[0]
    assert "### New features" in unreleased and "### Bug fixes" in unreleased and "- " not in unreleased
    assert release.notes(out, "1.0.0").strip() == "### New features\n\n- First"


def test_close_changelog_refuses_empty_or_repeated_releases():
    empty = release.close_changelog(LOG, "1.1.0", date(2026, 11, 2))
    with pytest.raises(release.ReleaseError, match="no new features or bug fixes"):
        release.close_changelog(empty, "1.2.0", date(2026, 11, 3))
    with pytest.raises(release.ReleaseError, match="already has a 1.0.0"):
        release.close_changelog(LOG, "1.0.0", date(2026, 11, 2))


def test_the_changelog_in_the_repository_can_be_released():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" in text
    for head in [line for line in text.splitlines() if line.startswith("### ")]:
        assert head in ("### New features", "### Bug fixes"), head


def test_versions_must_look_like_1_2_3():
    assert release.check_version("v1.2.3") == "1.2.3"
    for bad in ("1.2", "1.2.3-rc1", "latest"):
        with pytest.raises(release.ReleaseError):
            release.check_version(bad)


def test_prepare_sets_the_version_file(tmp_path):
    f = tmp_path / "_version.py"
    f.write_text('"""doc"""\n\n__version__ = "0.1.0"\n')
    release.set_version("1.0.0", f)
    assert release.current_version(f) == "1.0.0"
    assert f.read_text().startswith('"""doc"""')


def test_newer_versions():
    assert updates.is_newer("1.0.1", "1.0.0")
    assert updates.is_newer("v1.10.0", "1.9.9")
    assert updates.is_newer("2", "1.9")
    assert not updates.is_newer("1.0.0", "1.0.0")
    assert not updates.is_newer("1.0", "1.0.0")
    assert not updates.is_newer("0.9.0", "1.0.0")
    assert not updates.is_newer("nightly", "1.0.0")


def test_check_once_a_day():
    assert updates.due(None)
    assert updates.due("garbage")
    assert updates.due("2026-10-16", date(2026, 10, 17))
    assert not updates.due("2026-10-17", date(2026, 10, 17))


def test_release_from_the_latest_redirect():
    r = updates.release_from_location("https://github.com/x/releases/tag/v1.2.0")
    assert r == updates.Release("1.2.0", "https://github.com/x/releases/tag/v1.2.0")
    assert updates.release_from_location("/rani-feldman/rdt/releases/tag/v1.3.0") == \
        updates.Release("1.3.0", f"{updates.RELEASES_URL}/tag/v1.3.0")
    assert updates.release_from_location("https://github.com/x/releases") is None     # no release yet
    assert updates.release_from_location("https://github.com/x/releases/tag/nightly") is None
    assert updates.release_from_location("") is None


def test_fetch_latest_gives_none_when_github_cannot_be_read(monkeypatch):
    def fail(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(updates._no_redirect, "open", fail)
    assert updates.fetch_latest() is None


def _answer(monkeypatch, status, location=""):
    sent = []

    def open_(req, timeout):
        sent.append(req)
        raise updates.urllib.error.HTTPError(req.full_url, status, "", {"Location": location}, None)
    monkeypatch.setattr(updates._no_redirect, "open", open_)
    return sent


def test_fetch_latest_reads_the_redirect_not_the_api(monkeypatch):
    sent = _answer(monkeypatch, 302, "https://github.com/rani-feldman/RDT/releases/tag/v9.0.0")
    assert updates.fetch_latest() == updates.Release("9.0.0", "https://github.com/rani-feldman/RDT/releases/tag/v9.0.0")
    assert sent[0].full_url == "https://github.com/rani-feldman/rdt/releases/latest"
    assert "api.github.com" not in sent[0].full_url


def test_fetch_latest_gives_none_on_a_rate_limit_or_error(monkeypatch):
    _answer(monkeypatch, 429)
    assert updates.fetch_latest() is None
    _answer(monkeypatch, 404)
    assert updates.fetch_latest() is None


def test_shipped_documents_are_found_in_the_repository():
    for name in appfiles.DOCS:
        assert appfiles.read_doc(name), name


def test_workspace_carries_its_file_format(tmp_path):
    ws = Workspace.load(ROOT / "examples" / "sim_bench.rdtw")
    path = ws.save(tmp_path / "bench.rdtw")
    assert re.search(rf"(?m)^format\s*=\s*{FORMAT}$", path.read_text(encoding="utf-8"))
    assert Workspace.load(path).warnings == []


def test_workspace_from_a_newer_rdt_loads_with_a_warning():
    raw = {"format": FORMAT + 1, "connection": {"transport": "tcp", "address": "127.0.0.1:5025"},
           "mcu": [{"id": 0, "name": "main"}], "something_new": 1}
    ws = Workspace.from_dict(raw)
    assert any("newer RDT" in w for w in ws.warnings)
    with pytest.raises(Exception, match="format"):
        Workspace.from_dict({**raw, "format": "two"})


CHANGELOG = """# Release notes

## Unreleased

### New features

- Not out yet.

## 1.2.0 (2026-11-02)

### New features

- Faster block reads.

### Bug fixes

## 1.1.0 (2026-10-25)

### Bug fixes

- The COM port list is rescanned.

## 1.0.0 (2026-10-17)

### New features

- First release.
"""


def test_changes_since_keeps_only_newer_versions():
    notes = updates.changes_since(CHANGELOG, "1.0.0")
    assert notes.startswith("## 1.2.0") and "## 1.1.0" in notes and "Faster block reads" in notes
    assert "COM port list" in notes
    assert "First release" not in notes and "Not out yet" not in notes
    assert "### Bug fixes\n\n## 1.1.0" not in notes          # an empty heading is left out
    assert updates.changes_since(CHANGELOG, "1.2.0") == ""
    assert updates.changes_since("", "1.0.0") == ""


def test_fetch_latest_adds_the_changes_when_newer(monkeypatch):
    _answer(monkeypatch, 302, "https://github.com/rani-feldman/rdt/releases/tag/v1.2.0")
    asked = []
    monkeypatch.setattr(updates, "fetch_changelog", lambda v, t=None: asked.append(v) or CHANGELOG)
    r = updates.fetch_latest(current="1.1.0")
    assert r.version == "1.2.0" and "Faster block reads" in r.notes and "COM port" not in r.notes
    assert asked == ["1.2.0"]
    assert updates.fetch_latest(current="1.2.0").notes == ""     # not newer: no second request
    assert asked == ["1.2.0"]


def test_changelog_comes_from_the_tag_not_the_api():
    url = updates.CHANGELOG_URL.format(tag="v1.2.0")
    assert url == "https://raw.githubusercontent.com/rani-feldman/rdt/v1.2.0/CHANGELOG.md"
