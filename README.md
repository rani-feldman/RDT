# RDT
Remote Debugging Tool

Remote debugger tool: read and write firmware variables and memory over a COM port or LAN, without a debugger.

See [docs/DESIGN_DOCUMENT.md](docs/DESIGN_DOCUMENT.md) for the requirements and the design.

## Install

Download `RDT-Setup-<version>.exe` from [Releases](https://github.com/rani-feldman/rdt/releases) and run it (Windows, per user, no admin rights, nothing else to install). What's new is in [CHANGELOG.md](CHANGELOG.md); how a release is made is in [docs/RELEASING.md](docs/RELEASING.md).

## Host and CLI

```
python -m pip install -e .[dev]
python -m sim &          # the simulated device, in its own terminal
rdt -t examples/sim_target.toml info
rdt -t examples/sim_target.toml read main:motor
rdt -t examples/sim_target.toml write main:motor.mode MODE_FAULT
rdt -w examples/sim_bench.rdtw watch     # a workspace: watch tabs, scaling, write limits
```

See [docs/CLI.md](docs/CLI.md) for the commands, variable names and expressions, the target file and workspaces, and how the host handles timeouts, write confirmation, silent MCUs and link loss.

## GUI

```
python -m sim &
rdt-gui examples/sim_bench.rdtw
```

One connect button that shows the link, a card per MCU with its link and ELF check and its variables, watch tabs with a debugger-style watch tree from the ELF, live chart with cursors, statistics, CSV logging, memory view, SCPI console, an SCPI profile editor and several themes. See [docs/GUI.md](docs/GUI.md).

## User manual

[docs/USER_MANUAL.md](docs/USER_MANUAL.md) walks through setting up a bench and operating RDT, from the GUI and from the command line. [A narrated video](docs/media/rdt-walkthrough.mp4) shows it in use (captions in [rdt-walkthrough.srt](docs/media/rdt-walkthrough.srt)), and [the presentation](docs/media/rdt-presentation.pdf) has the manual as slides; both are also in the GUI's Help menu.

## Simulated device

Until the firmware has the memory commands, RDT is developed against a simulated device that runs on the same PC:

```
python -m pip install -e .[dev]
python -m sim -v
python -m pytest
```

See [docs/SIMULATOR.md](docs/SIMULATOR.md). To try RDT on your own firmware's ELF, with a ready STM32H7 single-MCU setup, see [docs/use-your-own-elf.md](docs/use-your-own-elf.md).

## Licence

RDT is released under the [MIT licence](LICENSE).
