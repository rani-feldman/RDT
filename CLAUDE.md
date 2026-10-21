# RDT for Claude Code

RDT (Remote Debugger Tool) is a Windows PC app (Python 3.11+, PySide6, pyqtgraph) that reads and
writes firmware variables and memory over a COM port or TCP/UDP, using the firmware's SCPI
commands, with symbols from each MCU's ELF file. The firmware (C, Keil armlink, one ELF/.axf per
MCU) is not in this repository; `sim/` is a simulated device that stands in for it.

## Setup and tests

```
python -m pip install -e .[dev]
python -m pytest -q                      # the whole suite; set QT_QPA_PLATFORM=offscreen with no display
python -m pytest -q tests/test_profile.py   # one file while iterating
python -m sim -v                         # simulated device on 127.0.0.1:5025
rdt-gui examples/sim_bench.rdtw          # GUI against the simulator
```

On Linux the Qt tests need `libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3`; the
SessionStart hook in `.claude/` installs them and the package in cloud sessions. The whole suite
takes about a minute and a half; run it before pushing.

## Firmware rules the code must keep

These come from the owner's real firmware; breaking one breaks the real device even when the
simulator and the tests pass.

- Addresses in SCPI commands are bare hex, always 8 digits, zero-padded (`08000000`); values are
  bare hex, also always 8 digits, zero-padded (`000002FF`). No `0x` prefix anywhere.
- Every read and write covers whole 4-byte aligned words (profile `align`). Small reads are
  trimmed; small writes are read-modify-write.
- Read replies are only comma-separated 4-byte hex words, such as `30303030,2F30302F`.
- A failed command gets no reply at all, so failure is a timeout, and every write is confirmed by
  reading it back.
- RDT never sends `SYST:ERR?` or `*IDN?` by itself (the user may type them in the console). Connected
  means the port or socket is open.
- Reads and writes are refused outside the ELF sections and the listed memory ranges; peripheral
  ranges are never merged into block reads.
- The ELF-to-device flash check refuses reads and writes on a mismatch; only CLI `--allow-mismatch`
  overrides it.
- Never send exploratory reads or writes to the owner's real device without their explicit go-ahead.

## Docs that change with the code

- `docs/DESIGN_DOCUMENT.md` is the single source of truth. A new feature gets a requirement row in
  section 2 (ID like `LNK-10`, text with who asked and the date, status, link) and a `D-<ID>` design
  section in section 4, plus a dated line in the decision log. Status moves Planned / In review /
  Done; mark it Done in a follow-up commit once the PR merges. The decision log is section 8.
- `CHANGELOG.md`: one line under `## Unreleased` in `### New features` or `### Bug fixes` for each PR
  that adds a feature or fixes a bug, written for someone using RDT. Tweaks, refactors, docs and
  tests get no line.
- `docs/USER_MANUAL.md`, `docs/GUI.md`, `docs/CLI.md`, `docs/SIMULATOR.md`: update whichever
  describe the behaviour you changed. Manual screenshots are in `docs/images/manual-*.png`.
- Releases are made by the `release` workflow; see `docs/RELEASING.md`.

## Code layout

See "Package layout" in `docs/DESIGN_DOCUMENT.md` section 3. Front ends (GUI, CLI) call the Target
API (`rdt/target.py`) only; `rdt/session.py` owns timeouts, retries, write confirmation and
reconnect; `rdt/profile.py` and `rdt/protocol/scpi.py` build the SCPI commands.

## Conventions

- Comments and docs are plain English and say why; requirement IDs and the owner's decisions are
  cited inline, such as `(rani, 2026-10-12)`.
- Per-user settings live in `ui.toml` under `%LOCALAPPDATA%\rdt` (`rdt/ui_settings.py`); don't
  add new settings files.
- New TOML output goes through `rdt/tomlw.py`.
