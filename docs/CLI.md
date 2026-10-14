# Host core and CLI

The host core (`rdt/`) is the part of RDT below the GUI: transports, the SCPI profile engine, the session, and the Target API that the CLI and the GUI both call. `rdt` is its command line. It works the same against the simulated device and the real firmware; only the target file changes.

## Install

```
python -m pip install -e .[dev]
```

This installs three commands: `rdt` (the host), `rdt-gui` (the window, see [GUI.md](GUI.md)) and `rdt-sim` (the simulated device, the same as `python -m sim`). `python -m rdt` and `python -m rdt.gui` also work.

## Try it against the simulator

In one terminal:

```
python -m sim
```

In another:

```
rdt -t examples/sim_target.toml info
rdt -t examples/sim_target.toml symbols main
rdt -t examples/sim_target.toml read main:motor motorctl:temperature
rdt -t examples/sim_target.toml write main:motor.mode MODE_FAULT
rdt -t examples/sim_target.toml dump main:firmware_name
rdt -t examples/sim_target.toml watch main:motor.speed motorctl:pwm_duty -i 100
rdt -t examples/sim_target.toml console
```

## Names and addresses

Every expression starts with its MCU's name, always: `main:motor.speed` is the variable `motor.speed` in the ELF of the MCU named `main`. There is no default MCU.

| Expression | Meaning |
| --- | --- |
| `main:tick_count` | A global variable |
| `main:motor.speed` | A struct or union member, to any depth |
| `main:gains[2]`, `main:matrix[1][2]` | An array cell |
| `main:*head`, `main:head->next->value`, `main:(*head).value`, `main:head[2]` | Following a pointer |
| `main:motor.c:state` | A file-static variable, named with its source file. Needed only when two files have a variable of that name |
| `@main:0x20000010:u32` | A raw address as one of the types `u8 i8 u16 i16 u32 i32 u64 i64 f32 f64` |
| `@main:0x48000000:GPIO_TypeDef.ODR` | A raw address as any type in the ELF (a typedef, or a struct, union or enum tag), then members and cells as above |

The names, addresses and types come from the ELF's DWARF debug info, so the firmware must be built with `-g`. RDT knows every global and file-static variable with a fixed address. Locals and function-static variables are not included, and neither are variables the compiler kept only as a constant. The byte order and pointer size come from the ELF.

Following a pointer reads it from the device. An expression that goes through a pointer is resolved again before every read, so `watch main:head->value` follows `head` as the firmware moves it.

Reading DWARF takes a while for a large firmware, so the result is cached, keyed by the ELF's hash: in `%LOCALAPPDATA%\rdt\cache` on Windows, `~/.cache/rdt` elsewhere, or `RDT_CACHE_DIR`. A rebuilt ELF gets a new entry; old entries can be deleted at any time.

### How values are shown

`read` shows a scalar on one line. A struct or array opens to every field and cell, nested to any depth, one line each with its full expression, so any line can be copied into `write`:

```
$ rdt -t examples/sim_target.toml read main:motor
main:motor  motor_t, 16 bytes at 0x20000000
main:motor.speed      = 1.5            float
main:motor.target     = 3000           float
main:motor.current_ma = -120 (0xFF88)  int16_t
main:motor.enabled    = 1 (0x01)       uint8_t
main:motor.mode       = MODE_RUN (1)   mode_t
```

- Integers in decimal and hex; floats; `true` and `false` for `bool`.
- Enums as `NAME (n)`.
- Bit fields as their own value, taken out of the word that holds them.
- `char` arrays as a string, such as `"rdt-main"`; `uint8_t` arrays stay numbers. A `char` also shows its character.
- Pointers as an address and what it points to, such as `0x20000048 (&nodes[1])` or `0x00000000 (NULL)`.
- An array shows its first 100 cells; `--max-cells N` changes that, and a cell further on can be read by its index.

A struct or array is read in one block, so all its fields come from the same moment, except on MCUs 1 and up, where a block larger than 64 bytes is read in pieces.

### Writing values

`write` takes one scalar: a number, a bool, an enum, a pointer or a bit field. A struct or array is written one field or cell at a time.

Values can be `42`, `-7`, `0x1F`, `0b101`, `1.5`, `2.5e3`, `true`/`false` for a bool, an enumerator's name such as `MODE_FAULT` (or its number), and `'A'` for a `char`. A hex integer written to a float is taken as its bit pattern, so `write @main:0x20000004:f32 0x3FC00000` writes 1.5. Values that don't fit the type, or the bit field's width, are refused before anything is sent.

A bit field is written by reading the word that holds it, changing the field and writing the word back, then reading it again to confirm. If the firmware changes a neighbouring field in that moment, its change is lost.

## Commands

| Command | What it does |
| --- | --- |
| `info` | Device identity, max response size, and each MCU's memory map and ELF identity check |
| `symbols MCU [PATTERN]` | List an MCU's variables with address, size and type. `PATTERN` is a glob such as `motor*`, or any part of the name. Needs no device |
| `ptype EXPR` | Where a variable is, and its type's layout: member offsets, bit positions, enum values. Also takes a type, such as `main:motor_t`. Needs no device |
| `read [EXPR...] [--tab NAME] [--max-cells N]` | Read values; structs and arrays open to every field and cell. With no names, every item in the workspace's watch tabs, or with `--tab` the items of one tab |
| `write EXPR VALUE [-y]` | Write one value, then read it back to confirm it. Goes through the workspace's scaling and write safety; `-y` answers yes when it asks |
| `dump EXPR [LEN]` or `dump MCU ADDR LEN` | Hex and ASCII dump of a variable or of a memory range |
| `poke MCU ADDR HEX` | Write raw bytes, lowest address first, and confirm them |
| `send COMMAND [--expect REGEX]` | Send one SCPI command. A query (header ending in `?`) waits for its reply; `--reply` and `--no-reply` override that. `--expect` checks the reply: exit code 11 if no part of it matches the regular expression or no reply came, such as `rdt send "*IDN?" --expect "^RDT,"` |
| `watch [EXPR...] [--tab NAME] [-i MS] [-n N] [--csv FILE]` | Poll values and print one line per sample. A struct or array becomes one column per field and cell. With no names, every item in the workspace's watch tabs (or with `--tab`, one tab's), each at its own interval. `--csv` also logs every value read to a CSV file (see [CSV log](#csv-log)) |
| `console [--timestamps] [--log FILE]` | Interactive SCPI console. `.read EXPR` reads, `.write EXPR VALUE` writes, `.history [N]` lists the saved history, `!N` sends entry N again, `.quit` leaves. See [Console](#console) |
| `commands list [TEXT]`, `commands import FILE [--replace]`, `commands export FILE`, `commands clear` | The workspace's list of known commands. See [Console](#console). Needs no device |
| `history [-n N] [--clear]` | List the last N (default 20, 0 for all) commands of the saved console history, or delete it. Needs no device |
| `init FILE` | Save the current target (file and command-line options) as a new workspace. Needs no device |
| `list` | Show the workspace's watch tabs and their items. Needs no device |
| `add EXPR [--tab NAME] [options]` | Add an item to a watch tab, or change one: `--format`, `--q`, `--scale`, `--offset`, `--unit`, `--poll`, `--read-only`, `--min`, `--max`, `--confirm`. `--tab` names the tab and makes it if it is new; without it, a new item goes to the first tab and an item already watched is changed where it is. Needs no device |
| `read-only [on\|off]` | Show or set the workspace's read-only default: with it on, every item refuses writes unless it was added with `--no-read-only`, and raw writes (`poke`, expressions not watched) are refused. Needs no device |
| `remove EXPR [--tab NAME]` | Remove an item from one watch tab, or without `--tab` from every tab. Needs no device |
| `tab add NAME`, `tab rename NAME NEW`, `tab remove NAME` | Add an empty watch tab, rename one, or remove one with its items. Needs no device |

`-v` logs link events and `-vv` logs every command and reply, which is the first thing to turn on when trying a new firmware. The exit code is 0 on success, 1 if anything failed, and 11 when `send --expect` gets no matching reply.

### Console

`rdt console` sends each line you type as an SCPI command; a query (header ending in `?`) waits for its reply. Lines starting with `.` are RDT's own commands (`.help` lists them).

- **History.** Every SCPI command sent is saved, per user, in `%LOCALAPPDATA%\rdt\console_history.txt` (the last 500; set `RDT_HISTORY_FILE` to use another file). The GUI console shares it. In a terminal, Up and Down walk through it. `.history` lists the last 20 numbered (`.history 50` the last 50), `!N` sends entry N again, and `.history clear` empties it. `rdt history` does the same without a device.
- **Known commands.** A workspace can hold a list of the device's commands (`[[command]]` tables). `rdt commands import FILE` fills it from a CSV file with the columns `command`, `name`, `group` and `description` (a header row names them in any order; without one they are taken in that order; commas, semicolons or tabs separate them), or from a text file with one command per line; lines starting with `#` are skipped. Commands already listed (ignoring case) are skipped, and `--replace` replaces the list. `rdt commands export FILE` writes it as CSV, `rdt commands list [TEXT]` shows it by group, and in the console `.commands [TEXT]` does the same.
- **`.info`** shows what `rdt info` shows, **`.reconnect`** closes the link and opens it again (after a board reset, for example), and **`.break`** sends a 250 ms break on a COM port link.
- **Tab completion.** Tab completes the line from the history, the imported known commands and the console's own `.` commands; pressed twice it lists the choices. This needs line editing: on Windows RDT installs `pyreadline3` for it.
- **`--timestamps`** starts every line with the time to the millisecond, and echoes each command as `> COMMAND` so it has a time too.
- **`--log FILE`** appends every line sent and received to a text file, each with its date and time and a mark: `>` sent, `<` reply, `!` error, `#` note.

`watch` and `console` keep running when the link drops, show `link-down`, and reconnect on their own. The other commands are one-shot and fail at once.

`watch` reads everything that is due in one poll, through the poll scheduler: items close to each other on the same MCU (at most 32 bytes apart) are merged into one read of up to `max_response` bytes, so they come from the same moment and cost one command. Items in a peripheral range are always read on their own, at their exact size. If a merged read fails, its items are read again one by one, so one bad item fails alone. When the link is slower than the interval, `watch` falls behind rather than queueing reads.

`watch` also notices when an MCU's ELF is rebuilt. It loads the new ELF and finds each watched item again by name, and prints a `#` line for each item that moved, changed type or no longer exists (its column then shows `gone`). An ELF the linker is still writing is left until it has been still for half a second, and one that can't be read keeps the old symbols.

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
identity = "firmware_version"     # optional: a constant that changes with every build, checked on connect

  [[mcu.ranges]]                  # memory with no ELF section behind it, such as peripherals
  name     = "GPIOA"
  start    = 0x48000000
  size     = 0x400
  writable = true

  [[mcu.ranges]]                  # a whole RAM bank: may overlap the ELF's .data and .bss
  name  = "DTCM"
  kind  = "memory"
  start = 0x20000000
  end   = 0x2001FFFF              # the last address in it; or give size

[[mcu]]
id         = 1
name       = "motorctl"
elf        = "build/motorctl.elf"
timeout_ms = 1000                 # overrides the profile's timeout for this MCU
```

The command line can override or replace it: `--tcp HOST:PORT`, `--udp HOST:PORT`, `--serial PORT --baud N`, `--profile FILE`, `--timeout MS`, `--allow-mismatch` (see [ELF identity](#what-the-host-does-with-the-firmwares-habits)), and `--mcu NAME=ID[:ELF]` (repeat for each MCU; replaces the file's MCUs). Without a target file, `--mcu` is required:

```
rdt --serial COM5 --profile fw_profile.toml --mcu main=0:build/main.elf read main:motor
```

A target file is also a workspace with no watch tabs; see [Workspace](#workspace).

### Default files

Without `-t` or `-w`, `rdt` uses `rdt.toml` from the current folder when there is one, so with your own bench in it, `rdt info` and `rdt read main:motor` need no options. Command-line options still override it. It can be a target file or a workspace; `add`, `remove` and `tab` save their changes into it, as for any workspace. `rdt-gui` opens it too when there is no last workspace to reopen. To start one for an STM32H7, from the repository root:

```
copy examples\stm32h7_interface.rdtw rdt.toml
```

and change `profile` to `"sim/examples/default_profile.toml"` (paths are relative to the file) and `elf` to your ELF, with the same MCU id, name and ELF as the simulator's `sim.toml` ([Default config](SIMULATOR.md#default-config-simtoml), [Use your own ELF](use-your-own-elf.md)). `rdt.toml` and `sim.toml` in the repository root are ignored by git.

## Workspace

A workspace (`.rdtw`) is a target file plus everything set up on top of it: the watch tabs, each a named watch list, with each item's format, scaling, polling and write safety, and the chart layout. Give it with `-w` (`-t` is the same option). [`examples/sim_bench.rdtw`](../examples/sim_bench.rdtw) is one for the simulator, with the tabs `Motor` and `System`:

```
rdt -w examples/sim_bench.rdtw read                 # every watched item, scaled
rdt -w examples/sim_bench.rdtw read --tab system    # one tab's items
rdt -w examples/sim_bench.rdtw watch                # each item at its own interval
rdt -w examples/sim_bench.rdtw write main:motor.target 2500    # asks first, checks 0..3000
rdt -w examples/sim_bench.rdtw add main:gains --tab Tuning     # makes the tab Tuning
```

Start one from a target file or command-line options with `rdt init bench.rdtw`, then build the watch tabs with `add`, `remove` and `tab`, or edit the file. RDT rewrites the whole file when it saves, so comments are not kept, and paths are saved relative to the file.

```toml
profile = "fw_profile.toml"   # or an inline [scpi] table, as in a target file
poll_ms = 500                 # global polling interval; 0 turns polling off

[connection]                  # and [[mcu]] tables: the same as a target file
...

[[tab]]
name = "Motor"                # one watch tab, in the order the GUI shows them

  [[tab.watch]]
  expr      = "main:motor.speed"
  poll      = 100             # "global" (the default), "manual", or ms
  format    = "dec"           # dec | hex | bin | char, for integers
  q         = "Q15"           # fixed point: Q15, Q31, Q3.12 ...
  scale     = 0.5             # shown = raw / 2^q * scale + offset
  offset    = 0
  unit      = "rpm"
  read_only = false
  min       = 0               # write limits, in shown units
  max       = 3000
  confirm   = true            # ask before writing

[[tab]]
name = "Overview"

  [[tab.watch]]
  expr = "main:motor.speed"   # also in this tab, with the settings given above

[chart]                       # the GUI's chart layout; kept as written

[[command]]                   # the device's known commands, for the console's list
command     = "MOTor:SPEed?"
name        = "Speed"         # name, group and description are optional
group       = "Motor"
description = "Motor speed in rpm"
```

Every key in a `[[tab.watch]]` but `expr` is optional.

- **Tabs.** Each tab has a name, unique whatever the case, and its own list of items. An item can be in several tabs, but it is still one item: its settings are the same in every tab and it is read once per poll. RDT writes its settings at its first place only; a later place may repeat them, but not differ. Every tab is polled, not only the one the GUI shows.
- **Older workspaces.** A file from before tabs has a plain `[[watch]]` list; it loads as one tab named `Watch` and is saved with tabs. Its `[[button]]` tables (command buttons, which RDT no longer has) are ignored with a warning and left out when the file is saved; send such commands with `rdt send` or the console.

- **Scaling** applies to a single value, not to a struct or array. A write takes the value in shown units and writes the nearest raw value. A value whose raw form doesn't fit the type is refused, and a float variable keeps the fraction.
- **Write safety.** A `read_only` item refuses writes. `read_only = true` at the top of the workspace makes every item read-only unless the item says `read_only = false`, and also refuses raw writes (`rdt read-only on|off` sets it). `min` and `max` are checked in shown units, and `confirm` asks before writing. Without a terminal to ask, or `-y`, the write is refused. A read-only or confirm-first struct or array covers its fields and cells too.
- **Polling.** `poll_ms` is the global interval, and each item can have its own interval or be `manual`. In `watch`, `-i` replaces the global interval, and a manual item is read once at the start.

### CSV log

`watch --csv FILE` (and **Log CSV** in the GUI) writes one row per poll:

```
time,seconds,main:motor.speed [rpm],main:motor.mode,main:tick_count,event
2026-10-11T14:03:22.120+03:00,0.000,1500.5,1,6911,
2026-10-11T14:03:22.220+03:00,0.100,1502.25,,,
2026-10-11T14:03:23.950+03:00,1.830,,,,link down
```

- `time` is the PC's clock in ISO 8601 with its UTC offset, and `seconds` counts from the first row.
- One column per value, struct fields and array cells included, with the unit in the header. Values are the shown ones: scaled, a bool as 0 or 1, an enum as its number. A `char` array is written as its string. Numbers are always decimal, whatever the item's display format.
- A cell is empty when that value was not read in that poll: it has a slower interval, it is manual, or the read failed.
- The `event` column marks `link down`, `link up` and `ELF rebuilt for MCU`.
- The file is UTF-8 with a byte order mark, so Excel shows units such as `°C` correctly.

## SCPI profile

In every command, `{addr}` and `{value}` are written in bare upper-case hex with no prefix: address `0x11AABBCCDD` is sent as `11AABBCCDD`, and a float 1.5 as its bit pattern `3FC00000`. A template may pad them, as in `{addr:08X}`, but not change the base. A read reply is one bare hex value per 4-byte word (`align` bytes), lowest address first, separated by commas, the way the firmware prints each word as a `uint32_t`: 8 bytes holding the floats 1.5 and 360.0 come back as `3FC00000,43B40000`. RDT turns each value into bytes in the MCU's byte order (from the ELF), so on a little-endian MCU `00000101` is the bytes `01 01 00 00` in memory. Leading zeros may be left out; a reply with a prefix such as `0x`, a word wider than `align` bytes, or the wrong number of words is refused. Older profiles may still have `addr_format`, `value_format`, `data_format`, `size_query`, `error_query` or `probe`; they are ignored.

Every read and write command covers whole aligned words of `align` bytes (4 by default; 1, 2, 4 or 8): the address is rounded down and the end rounded up to a multiple of `align`. A 2-byte variable at `20000006` is read with `MEMory:READ? 0,20000004,4`, and RDT keeps the 2 bytes asked for. `max_write` and `max_response` must be at least `align`.

The profile is the same `[scpi]` table the simulator reads ([`default_profile.toml`](../sim/examples/default_profile.toml)); see [D-SCPI-1](DESIGN_DOCUMENT.md#d-scpi-1-scpi-profile) in the design. The host also reads these keys, which the simulator ignores:

| Key | Default | Meaning |
| --- | --- | --- |
| `timeout_ms` | 500 | How long to wait for a reply |
| `retries` | 2 | Extra attempts after a read times out, with a growing delay |
| `reply_regex` | none | For a read reply with text around the data; the group named `data` (or group 1) holds the comma-separated words |
| `byte_order` | `little` | Used for an MCU without an ELF |

A profile may also list `[[ranges]]` that apply to every MCU, or to one MCU with `mcu = "name"`.

A range is a `peripheral` (the default) or `memory`:

| | `kind = "peripheral"` | `kind = "memory"` |
| --- | --- | --- |
| For | Register blocks | RAM and flash banks, such as the firmware's own table of readable regions |
| Overlapping the ELF's sections or other ranges | Refused when the file is loaded | Allowed |
| Reads | Each item on its own, at its exact size, never merged | Merged with nearby items like the ELF's sections |
| An access that runs past its end | Refused | Refused, unless another section or memory range continues it |
| `writable = false` | Writes refused | Writes refused; a read-only ELF section inside a writable memory range stays read-only |

Give a range's extent as `size`, or as `end`, the last address in it, the way firmware tables often write it.

## What the host does with the firmware's habits

- **No reply on failure.** A failed command sends no reply. After a read times out, RDT re-sends it with a growing delay. RDT sends no query of its own (no error query, no `*IDN?`); to see why a command failed, send `SYST:ERR?` yourself (`rdt send` or the console).
- **Writes are confirmed.** A write gets no reply either way, so RDT reads the value back after every write. A mismatch is an error that shows both values. Over UDP a write that didn't land is re-sent, but only after the read-back showed it missing.
- **ELF identity.** On connect and `.reconnect`, and before the next write after the link or an MCU came back from a drop, RDT compares each MCU's `identity` variable (or the GNU build ID, when the linker put it in memory) on the device with the ELF file. On a mismatch it warns on stderr and refuses writes to that MCU; `--allow-mismatch` writes anyway. `rdt info` shows each MCU's result, and `watch` prints a `#` line when a rebuilt ELF does not match. To add a build ID to your firmware, see [Use your own ELF](use-your-own-elf.md#check-that-the-device-runs-this-elf).
- **Known memory only.** RDT refuses an access outside the MCU's ELF sections and listed ranges before sending anything, so a read that starts inside a region and runs past its end is never sent. Writes to read-only sections (`.text`, `.rodata`) are refused. An access that touches a peripheral range must stay inside it.
- **Large reads** are split into pieces of at most `max_response` bytes (rounded down to a multiple of `align`). `max_response` is set in the SCPI profile; RDT never asks the device for it.
- **Aligned words.** Reads and writes are always whole `align`-byte words. A write smaller than a word, or not on a word boundary, is a read-modify-write: RDT reads the word, puts the new bytes in, writes the whole word back and confirms only the bytes it was asked to write. If the firmware changes a neighbouring value in that word between the read and the write, the change is overwritten. The extra bytes of a word may lie outside the ELF's sections, but never in a peripheral range the access isn't in, and a read-modify-write never writes them into read-only memory.
- **Values wider than `max_write`** are written in aligned pieces, one write command each. These are not atomic.
- **Silent MCU.** When a secondary MCU stops answering (a `-240` error reply, or three timeouts in a row), only that MCU is marked silent. Its reads fail at once and its writes are refused; it is tried again every 2 s. The other MCUs keep working.
- **Link loss.** The link is up while the COM port or socket is open; RDT sends nothing to check the device on connect. If the port or socket breaks, the link goes down: accesses fail with "link down" and writes are refused. In `watch`, `console` and the GUI, RDT reconnects in the background with a growing delay.
- **Shared link.** Each read reply is checked against the requested size, and anything left over from an earlier command is dropped before each new one, so a late or stray reply is never taken for the current one.

## Python API

The CLI is a thin layer over `rdt.Target`, which the GUI will use too:

```python
from rdt import Target

with Target.from_file("examples/sim_target.toml") as t:
    motor = t.read_value("main:motor")                    # {'speed': 1.5, ..., 'mode': MODE_RUN (1)}
    t.write_value("main:motor.mode", "MODE_FAULT")        # confirmed by read-back
    t.write_value("motorctl:pwm_duty", 750)
    speed = t.read_value("@main:0x20000000:f32")          # a raw address
    loc = t.resolve("main:head->value")                   # address, type; resolve again each read if loc.dynamic
    block = t.read("motorctl", 0x20000000, 140)           # bytes
    print(t.command("*IDN?"))                             # any SCPI command
```

`rdt.scheduler.read_many(t, exprs)` reads many items in as few reads as possible and returns one `Sample` (bytes, PC time, or the error) per item. `rdt.stats.StatsBook` keeps min, max, mean and count per value, and `rdt.csvlog.CsvLog` writes the CSV log.

`read_value` returns a number or bool for a scalar, an `EnumValue` (an `int` with a `.name`) for an enum, `bytes` for a `char` array, a dict for a struct and a list for an array. `read_rows` gives the display rows `rdt read` prints, which the GUI's variable view will use too. `refresh_elfs()` reloads rebuilt ELFs and `remap(loc)` finds a watched item again by name.

A workspace adds its watch tabs, scaling and write safety on top:

```python
from rdt import Target, Watch, Workspace

ws = Workspace.load("examples/sim_bench.rdtw")
for tab in ws.tabs:                                        # each a name and its list of items
    print(tab.name, [w.expr for w in tab.watches])
ws.add(Watch("main:gains"), "System")                      # also add_tab, rename_tab, remove_tab, remove
with Target(ws.config) as t:
    for w in ws.watches:                                   # every item once, in tab order
        loc, shown = ws.read(t, w)                         # scaled, in the item's format
    for r in ws.read_many(t, ws.watches):                  # one poll: nearby items share a read
        print(r.time, r.watch.expr, r.error or [s.text for s in r.shown])
    ws.write(t, "main:motor.target", "2500", confirm=lambda q: input(q) == "y")
    ws.save()
```

Errors are all `rdt.RdtError`: `AccessRefused`, `WriteRefused` (the workspace's write safety), `SymbolError` (a name, member or index that doesn't resolve), `CommandTimeout`, `WriteNotConfirmed`, `McuSilent`, `LinkDown`, `ConfigError` and a few more in [`rdt/errors.py`](../rdt/errors.py).

## Code layout

```
rdt/
  transport/   base.py, tcp.py, udp.py, serial.py   byte movers
  protocol/    scpi.py                              profile commands, reply framing
  profile.py                                        SCPI profile: templates, reply formats
  session.py                                        timeouts, retries, write confirm, silent MCUs, reconnect
  memmap.py                                         ELF sections and listed ranges; access checks
  symbols/     elf.py, types.py, expr.py, values.py DWARF symbol table, type model, expressions, typed values
  codec.py                                          raw scalar types
  target.py                                         Target API and target file
  scheduler.py                                      poll scheduler: block merging, one poll for many items
  stats.py, csvlog.py                               min/max/mean per value, the CSV log
  workspace.py, scaling.py, tomlw.py                workspace files, display formats and scaling, write safety
  console.py                                        console history, autocomplete, session log, known commands
  cli.py                                            command line
  gui/         app.py, device.py, variables.py, panels.py, chart.py   the GUI (PySide6): main window, device worker thread,
                                                    watch tabs and symbol browser, memory view, console, dialogs,
                                                    live chart (pyqtgraph)
```

## Tests

`python -m pytest` runs the host tests against the simulator over TCP, UDP (with dropped datagrams) and a serial pseudo-terminal on Linux. They cover every reply format, user-defined commands, split reads and writes, write confirmation, a silent MCU, a cut link and reconnect, and a second SCPI client sharing the device and its error queue.

The symbol tests use a corpus in [`tests/elf`](../tests/elf): one C program with structs, unions, anonymous members, bit fields, enums, 2-D arrays, pointer chains, function pointers and clashing file-statics, built by clang for little-endian Cortex-M4 (DWARF 4), by clang for big-endian ARM (DWARF 5), and by GCC for x86-64 (64-bit pointers). A second "rebuilt" image tests reloading. `build.sh` rebuilds them; the ELFs are committed.

`tests/test_gui.py` drives the GUI against the simulator with pytest-qt, offscreen (`QT_QPA_PLATFORM=offscreen`; on Linux it needs `libegl1`, `libxkbcommon0` and `libfontconfig1`).

### On Windows over a COM port

Install com0com, run `python -m sim --serial COM11`, then:

```
rdt -t examples/sim_target.toml --serial COM12 info
```
