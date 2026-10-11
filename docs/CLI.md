# Host core and CLI

The host core (`rdt/`) is the part of RDT below the GUI: transports, the SCPI profile engine, the session, and the Target API that the CLI and the GUI both call. `rdt` is its command line. It works the same against the simulated device and the real firmware; only the target file changes.

## Install

```
python -m pip install -e .[dev]
```

This installs two commands: `rdt` (the host) and `rdt-sim` (the simulated device, the same as `python -m sim`). `python -m rdt` also works.

## Try it against the simulator

In one terminal:

```
python -m sim
```

In another:

```
rdt -t examples/sim_target.toml info
rdt -t examples/sim_target.toml read @main:0x20000010:u32 @motorctl:0x20000088:f32
rdt -t examples/sim_target.toml write @main:0x20000004:f32 2500
rdt -t examples/sim_target.toml dump main 0x20000000 64
rdt -t examples/sim_target.toml watch @main:0x20000000:f32 -i 100
rdt -t examples/sim_target.toml console
```

## Addresses

Every address names its MCU, always: `@main:0x20000010:u32` reads 4 bytes at `0x20000010` on the MCU named `main` and shows them as an unsigned 32-bit integer. Types are `u8 i8 u16 i16 u32 i32 u64 i64 f32 f64`. The byte order comes from the MCU's ELF.

Variable names such as `main:motor.speed` come with ELF symbol support in M3.

Values for `write` can be `42`, `-7`, `0x1F`, `0b101`, `1.5` or `2.5e3`. A hex integer written to a float is taken as its bit pattern, so `write @main:0x20000004:f32 0x3FC00000` writes 1.5.

## Commands

| Command | What it does |
| --- | --- |
| `info` | Device identity, max response size, and each MCU's memory map |
| `read REF...` | Read one or more values |
| `write REF VALUE` | Write a value, then read it back to confirm it |
| `dump MCU ADDR LEN` | Hex and ASCII dump |
| `poke MCU ADDR HEX` | Write raw bytes, lowest address first, and confirm them |
| `send COMMAND` | Send one SCPI command. A query (header ending in `?`) waits for its reply; `--reply` and `--no-reply` override that |
| `errors` | Read the SCPI error queue until it is empty |
| `watch REF... [-i MS] [-n N]` | Poll values and print one line per sample |
| `console` | Interactive SCPI console. `.err` reads the error queue, `.read REF` reads a value, `.quit` leaves |

`-v` logs link events and `-vv` logs every command and reply, which is the first thing to turn on when trying a new firmware. The exit code is 0 on success and 1 if anything failed.

`watch` and `console` keep running when the link drops, show `link-down`, and reconnect on their own. The other commands are one-shot and fail at once.

## Target file

A target file says how to reach the device, which SCPI profile it speaks, and which MCUs it has. [`examples/sim_target.toml`](../examples/sim_target.toml) is the one for the simulator. For a real bench, copy it and change three things:

```toml
profile = "fw_profile.toml"       # the firmware's SCPI profile (or an inline [scpi] table)

[connection]
transport = "tcp"                 # tcp | udp | serial
address   = "192.168.1.20:5025"
# transport = "serial"
# port      = "COM5"
# baud      = 115200

[[mcu]]
id   = 0                          # MCU 0 is the main MCU, which owns the link
name = "main"
elf  = "build/main.elf"

  [[mcu.ranges]]                  # memory with no ELF section behind it, such as peripherals
  name     = "GPIOA"
  start    = 0x48000000
  size     = 0x400
  writable = true

[[mcu]]
id         = 1
name       = "motorctl"
elf        = "build/motorctl.elf"
timeout_ms = 1000                 # overrides the profile's timeout for this MCU
```

The command line can override or replace it: `--tcp HOST:PORT`, `--udp HOST:PORT`, `--serial PORT --baud N`, `--profile FILE`, `--timeout MS`, and `--mcu NAME=ID[:ELF]` (repeat for each MCU; replaces the file's MCUs). Without a target file, `--mcu` is required:

```
rdt --serial COM5 --profile fw_profile.toml --mcu main=0:build/main.elf read @main:0x20000010:u32
```

The target file is the start of the `.rdtw` workspace that M3 adds.

## SCPI profile

The profile is the same `[scpi]` table the simulator reads ([`default_profile.toml`](../sim/examples/default_profile.toml)); see [SCPI profile](DESIGN.md#scpi-profile) in the design. The host also reads these keys, which the simulator ignores:

| Key | Default | Meaning |
| --- | --- | --- |
| `timeout_ms` | 500 | How long to wait for a reply |
| `retries` | 2 | Extra attempts after a read times out, with a growing delay |
| `probe` | `"*IDN?"` | Sent on connect and reconnect to check that the device answers. Empty to skip |
| `reply_regex` | none | For a read reply the fixed formats can't describe; the group named `data` (or group 1) is decoded with `data_format` |
| `byte_order` | `little` | Used for an MCU without an ELF |

A profile may also list `[[ranges]]` that apply to every MCU, or to one MCU with `mcu = "name"`.

## What the host does with the firmware's habits

- **No reply on failure.** A failed command sends no reply. After a read times out, RDT sends the error query once and reports its answer as a hint, then re-sends the read with a growing delay. The error queue is shared with other tools on the link, so the hint may be someone else's error.
- **Writes are confirmed.** A write gets no reply either way, so RDT reads the value back after every write. A mismatch is an error that shows both values. Over UDP a write that didn't land is re-sent, but only after the read-back showed it missing.
- **Known memory only.** RDT refuses an access outside the MCU's ELF sections and listed ranges before sending anything. Writes to read-only sections (`.text`, `.rodata`) are refused. An access that touches a listed range must stay inside it.
- **Large reads** are split into pieces of at most `max_response` bytes. If the profile has a `size_query`, its reply on connect sets `max_response`.
- **Values wider than `max_write`** are written in aligned pieces, one write command each. These are not atomic.
- **Silent MCU.** When a secondary MCU stops answering (the `-240` error, or three timeouts in a row while MCU 0 answers the error query), only that MCU is marked silent. Its reads fail at once and its writes are refused; it is tried again every 2 s. The other MCUs keep working.
- **Link loss.** If the connection breaks, or the device stops answering even the error query, the link goes down: accesses fail with "link down" and writes are refused. In `watch`, `console` and the GUI, RDT reconnects in the background with a growing delay.
- **Shared link.** Each read reply is checked against the requested size, and anything left over from an earlier command is dropped before each new one, so a late or stray reply is never taken for the current one.

## Python API

The CLI is a thin layer over `rdt.Target`, which the GUI will use too:

```python
from rdt import Target

with Target.from_file("examples/sim_target.toml") as t:
    speed = t.read_value("@main:0x20000000:f32")
    t.write_value("@motorctl:0x20000084:u32", 750)        # confirmed by read-back
    block = t.read("motorctl", 0x20000000, 140)           # bytes
    print(t.command("*IDN?"))                             # any SCPI command
```

Errors are all `rdt.RdtError`: `AccessRefused`, `CommandTimeout` (with `.hint`), `WriteNotConfirmed`, `McuSilent`, `LinkDown`, `ConfigError` and a few more in [`rdt/errors.py`](../rdt/errors.py).

## Code layout

```
rdt/
  transport/   base.py, tcp.py, udp.py, serial.py   byte movers
  protocol/    scpi.py                              profile commands, reply framing
  profile.py                                        SCPI profile: templates, reply formats
  session.py                                        timeouts, retries, write confirm, silent MCUs, reconnect
  memmap.py                                         ELF sections and listed ranges; access checks
  codec.py                                          scalar types
  target.py                                         Target API and target file
  cli.py                                            command line
```

## Tests

`python -m pytest` runs the host tests against the simulator over TCP, UDP (with dropped datagrams) and a serial pseudo-terminal on Linux. They cover every reply format, user-defined commands, split reads and writes, write confirmation, a silent MCU, a cut link and reconnect, and a second SCPI client sharing the device and its error queue.

### On Windows over a COM port

Install com0com, run `python -m sim --serial COM11`, then:

```
rdt -t examples/sim_target.toml --serial COM12 info
```
