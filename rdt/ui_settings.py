"""Per-user interface settings (``ui.toml``): how the GUI looks, kept apart from any workspace.

The file sits next to the console history (``%LOCALAPPDATA%\\rdt\\ui.toml`` on Windows) and
holds the console's text size and wrapping and the memory view's display format. It also
remembers the last workspace, to reopen it (WSP-3); that is not a setting, so it is not exported
and an import leaves it as it is.

    ui = UiSettings.load()
    ui.value("console/font_size", 10)
    ui.set_value("console/wrap", True)       # saved at once
    ui.export("my-ui.toml")
    ui.import_file("my-ui.toml")

A file that can't be written leaves the settings in memory only, as the console history does.
Each change re-reads the file before writing it, so two windows keep each other's changes.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from . import tomlw
from .console import history_path
from .errors import ConfigError

log = logging.getLogger(__name__)

# "section/key": the type its value must have
SETTINGS: dict[str, type] = {
    "console/font_size": int,
    "console/wrap": bool,
    "memory/kind": str,
    "memory/type": str,
    "memory/decimal": bool,
    "ui/theme": str,          # system, light or dark (GUI-15)
}
STATE: dict[str, type] = {
    "session/last_workspace": str,   # not a setting: never exported, kept on import
    "session/layout": str,           # the panels' layout, as QMainWindow.saveState in base64 (GUI-14)
    "session/watch_shown": bool,     # whether the watch tabs were shown
}
KNOWN = {**SETTINGS, **STATE}


def ui_path() -> Path:
    """The per-user settings file: ``ui.toml`` next to the console history."""
    if os.environ.get("RDT_UI_FILE"):
        return Path(os.environ["RDT_UI_FILE"])
    return history_path().parent / "ui.toml"


def workspace_dir() -> Path:
    """Where workspaces are kept by default: the folder of ui.toml (%LOCALAPPDATA%\\rdt on
    Windows), so a workspace's .rdtw, .watch.toml and .commands.toml sit beside the user's
    settings (rani, 2026-10-14)."""
    return ui_path().parent


class UiSettings:
    def __init__(self, path: str | Path | None = None, values: dict | None = None):
        self.path = Path(path) if path is not None else ui_path()
        self.values: dict[str, object] = dict(values or {})

    @classmethod
    def load(cls, path: str | Path | None = None) -> UiSettings:
        """The settings in the file; a missing or broken file gives no settings (the defaults)."""
        ui = cls(path)
        if ui.path.is_file():
            try:
                ui.values = {k: v for k, v in _read(ui.path).items() if _fits(k, v)}
            except ConfigError as e:
                log.warning("%s; using the default interface settings", e)
        return ui

    def exists(self) -> bool:
        return self.path.is_file()

    def value(self, key: str, default=None):
        return self.values.get(key, default)

    def set_value(self, key: str, value) -> None:
        """Set one setting and save the file. The file is read again first, so two windows (or a
        GUI and the CLI) each keep what the other set."""
        if key not in KNOWN:
            raise KeyError(f"no interface setting {key!r}")
        value = KNOWN[key](value)
        if self.values.get(key) == value:
            return
        if self.path.is_file():
            self.values = UiSettings.load(self.path).values
        self.values[key] = value
        self.save()

    def save(self) -> bool:
        try:
            _write(self.path, self.values, "RDT interface settings, per user. Saved by RDT, so comments are not kept.")
        except OSError as e:
            log.warning("cannot save interface settings %s: %s", self.path, e.strerror)
            return False
        return True

    def export(self, path: str | Path) -> Path:
        """Write the settings (not the last workspace) to a file of their own."""
        path = Path(path)
        try:
            _write(path, {k: v for k, v in self.values.items() if k in SETTINGS}, "RDT interface settings.")
        except OSError as e:
            raise ConfigError(f"cannot write {path}: {e.strerror}") from None
        return path

    def import_file(self, path: str | Path) -> list[str]:
        """Take the settings in an exported file: each one in it replaces the one here, and one
        it leaves out goes back to its default. Returns the keys taken."""
        path = Path(path)
        new = _read(path)
        bad = [k for k in new if k not in SETTINGS]
        if bad:
            raise ConfigError(f"{path}: not an interface setting: {', '.join(sorted(bad))}")
        for k, v in new.items():
            if not _fits(k, v):
                raise ConfigError(f"{path}: {k} must be {_type_name(SETTINGS[k])}, not {v!r}")
        self.values = {**{k: v for k, v in self.values.items() if k in STATE}, **new}
        self.save()
        return sorted(new)


def _fits(key: str, value) -> bool:
    t = KNOWN.get(key)
    if t is None:
        return False
    if t is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, t)


def _type_name(t: type) -> str:
    return {int: "a whole number", bool: "true or false", str: "text"}[t]


def _read(path: Path) -> dict[str, object]:
    """The file as flat keys: [console] font_size = 10 is "console/font_size"."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    out = {}
    for section, table in raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: {section} must be a [{section}] table")
        for k, v in table.items():
            out[f"{section}/{k}"] = v
    return out


def _write(path: Path, values: dict[str, object], header: str) -> None:
    tables: dict[str, dict] = {}
    for key in KNOWN:   # a fixed order, whatever order they were set in
        if key in values:
            section, name = key.split("/", 1)
            tables.setdefault(section, {})[name] = values[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomlw.dumps(tables, header), encoding="utf-8")
    os.replace(tmp, path)
