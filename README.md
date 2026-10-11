# RDT
Remote Debugging Tool

Remote debugger tool: read and write firmware variables and memory over a COM port or LAN, without a debugger.

See [docs/DESIGN.md](docs/DESIGN.md) for the design.

## Host and CLI

```
python -m pip install -e .[dev]
python -m sim &          # the simulated device, in its own terminal
rdt -t examples/sim_target.toml info
rdt -t examples/sim_target.toml read main:motor
rdt -t examples/sim_target.toml write main:motor.mode MODE_FAULT
```

See [docs/CLI.md](docs/CLI.md) for the commands, variable names and expressions, the target file, and how the host handles timeouts, write confirmation, silent MCUs and link loss.

## Simulated device

Until the firmware has the memory commands, RDT is developed against a simulated device that runs on the same PC:

```
python -m pip install -e .[dev]
python -m sim -v
python -m pytest
```

See [docs/SIMULATOR.md](docs/SIMULATOR.md).
