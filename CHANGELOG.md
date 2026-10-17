# Release notes

Each pull request that adds a feature or fixes a bug adds one line under **Unreleased**, in
**New features** or **Bug fixes**. Small changes (tweaks, refactors, documentation, tests) get no
line. A release renames Unreleased to the version and date, and the GitHub release shows that
section. RDT shows this file in **Help > Release notes**.

## Unreleased

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

### Bug fixes
