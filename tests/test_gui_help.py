"""The Help menu and the update link in the status bar (INS-7, INS-9, INS-12, INS-13)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from rdt import updates  # noqa: E402
from rdt._version import __version__  # noqa: E402
from rdt.gui import crash, help as helpmenu  # noqa: E402
from rdt.gui.app import MainWindow  # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("RDT_UI_FILE", str(tmp_path / "ui.toml"))
    w = MainWindow()
    qtbot.addWidget(w, before_close_func=lambda w: setattr(w, "dirty", False))
    return w


def help_actions(win):
    for top in win.menu_bar.actions():
        if top.text().replace("&", "") == "Help":
            return {a.text().replace("&", ""): a for a in top.menu().actions() if a.text()}
    raise AssertionError("no Help menu")


def test_help_menu_has_the_documents_and_about(win):
    names = set(help_actions(win))
    assert {"User manual", "Keyboard shortcuts", "Release notes", "Check for updates",
            "Check for updates at start", "Licence", "Third-party notices", "About RDT"} <= names


def test_release_notes_and_manual_open(win):
    notes = win.show_doc("RDT release notes", "changelog")
    assert "Unreleased" in notes.browser.toPlainText()
    manual = win.show_doc("RDT user manual", "manual")
    assert "RDT User Manual" in manual.browser.toPlainText()


def test_about_names_the_version(win):
    assert f"RDT {__version__}" in helpmenu.about_text()
    box = helpmenu.about(win)
    assert __version__ in box.text()
    box.close()


def test_status_bar_shows_a_newer_release_only(win):
    win.show_update("0.0.1")
    assert win.update_label.isHidden()
    win.show_update("999.0.0", "https://example/r")
    assert not win.update_label.isHidden()
    assert "999.0.0" in win.update_label.text() and "https://example/r" in win.update_label.text()


def test_update_check_saves_what_it_found(win, monkeypatch):
    shown = []
    monkeypatch.setattr(win, "show_update_dialog", lambda: shown.append(1))
    release = updates.Release("999.0.0", "https://example/r", "## 999.0.0\n\n- Faster reads.")
    win._update_checked(release)
    assert win.ui.value("updates/latest") == "999.0.0"
    assert not updates.due(win.ui.value("updates/last_check"))
    assert not win.update_label.isHidden()
    assert shown == [1]                          # what changed shows once on its own...
    win._update_checked(release)
    assert shown == [1]                          # ...not again the next day for the same version
    win._manual_check = True
    win._update_checked(release)
    assert shown == [1, 1]                       # Help > Check for updates always shows it


def test_update_without_its_notes_shows_again_next_time(win, monkeypatch):
    """L18: when the release notes couldn't be read, the version isn't marked as shown, so the
    next check (with the notes, normally) still pops it up."""
    shown = []
    monkeypatch.setattr(win, "show_update_dialog", lambda: shown.append(1))
    win._update_checked(updates.Release("999.0.0", "https://example/r"))
    assert shown == [1] and win.ui.value("updates/notified") is None
    win._update_checked(updates.Release("999.0.0", "https://example/r", "## 999.0.0\n\n- Faster reads."))
    assert shown == [1, 1] and win.ui.value("updates/notified") == "999.0.0"


def test_update_window_is_reused(win):
    """L1: each status bar click or Help > Check for updates raises the one window."""
    win._latest_release = updates.Release("999.0.0", "https://example/r", "## 999.0.0\n\n- Faster reads.")
    first = win.show_update_dialog()
    assert win.show_update_dialog() is first
    assert len([w for w in win.findChildren(helpmenu.UpdateDialog) if w.isVisible()]) == 1
    win._latest_release = updates.Release("999.1.0", "https://example/r2", "## 999.1.0\n\n- More.")
    second = win.show_update_dialog()            # a newer release: its own notes
    assert "999.1.0" in second.browser.toPlainText()
    assert len([w for w in win.findChildren(helpmenu.UpdateDialog) if w.isVisible()]) == 1
    second.close()


def test_update_dialog_shows_what_changed(win):
    win._latest_release = updates.Release("999.0.0", "https://example/r", "## 999.0.0\n\n- Faster reads.")
    dlg = win.show_update_dialog()
    text = dlg.browser.toPlainText()
    assert "RDT 999.0.0 is available" in text and "Faster reads." in text
    dlg.close()
    win._latest_release = updates.Release("999.0.0", "https://example/r")      # changes unknown
    dlg = win.show_update_dialog()
    assert "release page" in dlg.browser.toPlainText()
    dlg.close()


def test_update_check_on_start_respects_the_setting(win, monkeypatch):
    started = []
    monkeypatch.setattr(win.update_check, "start", lambda: started.append(1))
    win.set_update_check(False)
    win.start_update_check()
    assert started == []
    win.set_update_check(True)
    win.start_update_check()
    assert started == [1]
    win.ui.set_value("updates/last_check", "2999-01-01")   # already checked today
    win.start_update_check()
    assert started == [1]


def test_manual_check_says_when_up_to_date(win, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    win._manual_check = True
    win._update_checked(updates.Release(__version__, "https://example/r"))
    assert shown and "latest version" in shown[0]
    win._manual_check = True
    win._update_checked(None)
    assert "Could not reach GitHub" in shown[1]


def test_crash_log_is_written(tmp_path, monkeypatch):
    import sys
    path = tmp_path / "logs" / "rdt.log"
    hook = sys.excepthook
    try:
        assert crash.install(path) == path
        monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = hook
        import logging
        for h in list(logging.getLogger().handlers):
            if getattr(h, "baseFilename", None) == str(path):
                logging.getLogger().removeHandler(h)
                h.close()
    assert "ValueError: boom" in path.read_text(encoding="utf-8")


def test_video_and_presentation_open_from_help(win, monkeypatch):
    """INS-16: Help opens the walkthrough video and the presentation, from the checkout here."""
    opened = []
    monkeypatch.setattr("rdt.gui.app.QDesktopServices.openUrl", lambda url: opened.append(url.toString()) or True)
    actions = help_actions(win)
    actions["Video walkthrough"].trigger()
    actions["Presentation"].trigger()
    # the video always opens from the repository (rani, 2026-10-29)
    assert opened[0] == "https://github.com/rani-feldman/rdt/blob/main/docs/media/rdt-walkthrough.mp4"
    assert opened[1].startswith("file://") and opened[1].endswith("docs/media/rdt-presentation.pdf")


def test_media_open_from_the_installed_copy(monkeypatch, tmp_path):
    """INS-17: the installed app opens the presentation it carries; the video opens on GitHub
    even when a copy is there (rani, 2026-10-29)."""
    from rdt import appfiles
    media = tmp_path / "docs" / "media"
    media.mkdir(parents=True)
    (media / "rdt-walkthrough.mp4").write_bytes(b"video")
    monkeypatch.setattr(appfiles.sys, "frozen", True, raising=False)
    monkeypatch.setattr(appfiles.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert appfiles.media_url("video") == (
        "https://github.com/rani-feldman/rdt/blob/main/docs/media/rdt-walkthrough.mp4")
    # a file missing from the install falls back to its page on GitHub
    assert appfiles.media_url("presentation") == (
        "https://github.com/rani-feldman/rdt/blob/main/docs/media/rdt-presentation.pdf")
    (media / "rdt-presentation.pdf").write_bytes(b"pdf")
    assert appfiles.media_url("presentation") == (media / "rdt-presentation.pdf").as_uri()
