# RDT
Remote Debugging Tool

Remote debugger tool: read and write firmware variables and memory over a COM port or LAN, without a debugger.

See [docs/DESIGN.md](docs/DESIGN.md) for the design.

## Simulated device

Until the firmware has the memory commands, RDT is developed against a simulated device that runs on the same PC:

```
python -m pip install -e .[dev]
python -m sim -v
python -m pytest
```

See [docs/SIMULATOR.md](docs/SIMULATOR.md).
