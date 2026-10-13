# RDT
Remote Debugging Tool

Remote debugger tool: read and write firmware variables and memory over a COM port or LAN, without a debugger.

See [docs/DESIGN_DOCUMENT.md](docs/DESIGN_DOCUMENT.md) for the requirements and the design.

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

Connection bar, watch tabs with a debugger-style watch tree from the ELF, live chart with cursors, statistics, CSV logging, memory view, SCPI console and an SCPI profile editor. See [docs/GUI.md](docs/GUI.md).

## Simulated device

Until the firmware has the memory commands, RDT is developed against a simulated device that runs on the same PC:

```
python -m pip install -e .[dev]
python -m sim -v
python -m pytest
```

See [docs/SIMULATOR.md](docs/SIMULATOR.md). To try RDT on your own firmware's ELF, with a ready STM32H7 single-MCU setup, see [docs/use-your-own-elf.md](docs/use-your-own-elf.md).
