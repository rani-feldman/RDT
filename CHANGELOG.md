# Release notes

Each pull request that adds a feature or fixes a bug adds one line under **Unreleased**, in
**New features** or **Bug fixes**. Small changes (tweaks, refactors, documentation, tests) get no
line. A release renames Unreleased to the version and date, and the GitHub release shows that
section. RDT shows this file in **Help > Release notes**.

## Unreleased

### New features

- When a newer RDT is out, a window shows what changed in it, with a Download button.

### Bug fixes

- The update check no longer uses GitHub's API, so many PCs on one office network can't hit GitHub's limit of 60 checks an hour.
- The COM port list is rescanned each time you open it, so unplugged ports go and newly plugged ones show.
- A reply that arrives just after a read timed out is no longer taken for the next read's reply, which could show another address's value or write wrong bytes beside a small value.
- A value is written only when you press Enter or leave the cell; a value changing while you edit it no longer writes the old value or replaces what you are typing (watch list and memory view).
- An item with a minimum or maximum refuses `nan` and infinity, and the item settings no longer round its scale, offset and limits.
- Memory view writes over a read-only watched item are refused, and over an item that asks before writing, you are asked first.
- Opening a workspace or pressing F5 while connecting cancels the connect cleanly, instead of leaving the old device attached or the Connect button stuck.
- Closing RDT during a long device job no longer crashes, and the port is closed.
- A device that stops answering slows each poll by a few timeouts, not 5 to 17 seconds.
- `--allow-mismatch` keeps working after a reconnect, three missed replies or an ELF rebuild, and a rebuilt ELF is checked before the next write.
- Command-line options such as `--profile`, `--timeout` and `--mcu` apply to that run only and are no longer saved into the workspace; `--mcu` no longer removes the other MCUs and `--profile` keeps the workspace's memory ranges.
- A memory map import that overlaps the ELF is refused, and a workspace already broken this way can be mended with `rdt memory import --replace`.
- The profile dialog's **Test read** is refused outside known memory and on an ELF mismatch, like any other read.
- A profile whose address field would not give 8 zero-padded digits, or a profile value of the wrong type, gives a clear error.
- A CSV log that can't be written (disk full, drive removed) stops with an error instead of silently, and 64-bit values are logged exactly.
- ELF files with mutually referencing structs, long chains of linked structs, or DWARF 3 packed bit fields (armcc) are read correctly.
- A bad theme or density in the settings no longer stops RDT from starting, and only one Update available window opens.
- Clear error messages instead of crashes for a bad float bit pattern, a bad number on the command line and a blank `rdt send`.

## 1.0.0 (2026-10-17)

### New features

- First release. RDT reads and writes firmware variables and memory over a COM port or TCP/UDP, using
  the firmware's SCPI commands, for one or more MCUs, each with its ELF file: watch tabs, live chart,
  statistics, CSV log, memory view, SCPI console, ELF-to-device check, simulator and command line.
- Windows installer (per user, no admin rights) and a portable zip, with Python and every package built in.
- Upgrading keeps your settings and workspaces; uninstalling asks whether to delete them.
- `rdt` on the PATH and `.rdtw` workspaces open in RDT on a double-click.
- RDT says in the status bar when a newer release is on GitHub (**Help > Check for updates**).
- **Help** menu: user manual, release notes, licence, third-party notices and About.
- Unexpected errors are shown in a window and written to `%LOCALAPPDATA%\rdt\logs\rdt.log`.
