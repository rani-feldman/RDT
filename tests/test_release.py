"""Versions, release notes and the update check (INS-7, INS-9, INS-11, INS-13, INS-14)."""

import importlib.util
import json
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


def test_release_from_github_answer():
    r = updates.release_from({"tag_name": "v1.2.0", "html_url": "https://github.com/x/releases/tag/v1.2.0"})
    assert r == updates.Release("1.2.0", "https://github.com/x/releases/tag/v1.2.0")
    assert updates.release_from({"tag_name": "v1.2.0", "draft": True}) is None
    assert updates.release_from({"tag_name": "v1.3.0-beta", "prerelease": True}) is None
    assert updates.release_from({"message": "Not Found"}) is None


def test_fetch_latest_gives_none_when_github_cannot_be_read(monkeypatch):
    def fail(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    assert updates.fetch_latest() is None


def test_fetch_latest_reads_the_api(monkeypatch):
    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"tag_name": "v9.0.0", "html_url": "https://example/r"}).encode()

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout: Answer())
    assert updates.fetch_latest() == updates.Release("9.0.0", "https://example/r")


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
