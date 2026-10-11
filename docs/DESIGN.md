# RDT design summary

RDT (Remote Debugger Tool) reads and writes a running device's firmware variables and raw memory over the link the product already has, a COM port or LAN, so a live system can be inspected and tuned without a JTAG/SWD probe.

The full, living design document is [RDT Design Document](https://claude.ai/code/artifact/01fb6748-f6db-46d8-a10b-bd96ece091f2). This file summarizes the decisions as of 2026-10-11.

## Scope

- **Now:** the PC application only. It is built and tested against a simulated device.
- **Later:** rani adds memory read/write SCPI commands to the existing firmware. From then on the application works on real hardware with no other changes.
- **Not in v1:** breakpoints, stepping, local (stack) variables, flashing, authentication, map files.

## Decisions

| Topic | Decision |
| --- | --- |
| Symbol input | ELF only (DWARF debug info). Map files are not supported, because they carry no type information. |
| Target MCU | Not fixed. Endianness and pointer size come from the ELF header. |
| Transports | COM port, LAN TCP, and LAN UDP. The user enters the port (COM name and baud, or IP and port). More channels can be added later as plug-ins. |
| Protocol | SCPI first, because the existing firmware already speaks it. A compact binary protocol comes later, for when a dedicated firmware agent exists. |
| SCPI commands | Not hard-coded. The user defines them in an SCPI profile. |
| Read | One command: `read startAddress,size`. |
| Write | One value per command: `write address,value,size`. Buffer writes may come later. |
| Host | Python with a Qt GUI (PySide6). The GUI is the primary interface, and the CLI and Python API sit on the same core. |
| Authentication | None. RDT is a tool for trusted benches and networks. |
| PC operating system | Windows only. The code stays portable, so Linux can be added later. |
| Firmware language | C only. The symbol reader does not handle C++. |
| Memory ranges | RDT refuses any address outside the ELF's RAM and flash sections and the extra ranges listed in the SCPI profile. |
| Peripheral reads | Never merged into blocks. Each item in a peripheral range is read on its own at its exact size. |
| Link loss | RDT reconnects on its own. Values grey out and writes are refused while the link is down. |
| Multiple MCUs | One ELF per MCU. The main MCU is MCU 0 and forwards requests to the others. See [Multiple MCUs](#multiple-mcus). |
| Shared SCPI port | Other tools may use the port over LAN at the same time. RDT tolerates unexpected replies and busy firmware. |

## Architecture

The PC application has four layers. Each layer talks only to the layer directly below it.

![PC application block diagram](images/pc-block-diagram.png)

| Block | Role |
| --- | --- |
| Target API | One entry point for the GUI and the CLI |
| Workspace | Loads and saves `.rdtw` files |
| Write guard | Read-only flag, min/max limits, confirm before writing |
| Symbol database | ELF/DWARF loading, type model, expressions |
| Value codec | Display formats, Q-format, scaling |
| Poll scheduler | Polling intervals, merging nearby reads into blocks |
| ELF file watcher | Reloads the ELF and re-maps watched items when it is rebuilt |
| Statistics | Min, max and mean per watched item |
| CSV logger | Timestamped, scaled values |
| Session | Sequencing, timeouts, retries |
| SCPI profile engine | Command templates, reply parsing |
| Binary protocol | Later, for a firmware agent |
| Device | The simulator now, the existing firmware later |

Every panel, and the CLI, calls the Target API and never the layers below it:
- The core resolves names through the symbol database, plans reads in the poll scheduler, and checks writes in the write guard.
- Only the protocol and transport layers know how bytes reach the device.

So swapping the simulator for the real firmware, or SCPI for the binary protocol, changes nothing above those two layers.

The firmware only ever sees an address and a byte count. Everything that knows about names and types lives on the PC.

## Symbol handling

- `pyelftools` walks `.debug_info`. Every global and file-static variable is recorded with its address, size and type.
- The type model covers base types, pointers, arrays, structs and unions with member offsets, enums with their names, bit fields, and typedefs.
- Supported expressions: `name`, `a.b.c`, `arr[3]`, `arr[2][5]`, `*ptr`, `ptr->m`, `file.c:name`, and the raw form `@0x20000418:u32`.
- Every expression starts with the MCU's name, always, with no default: `main:motor.speed`, `@motorctl:0x20000418:u32`. The name is the MCU's display name from the workspace, and the watch list, chart legend and CSV columns show it too.
- A raw address can take any ELF type, then members or cells: `@main:0x48000000:GPIO_TypeDef.ODR`. The type can be a typedef or a struct, union or enum tag.
- An expression that goes through a pointer is re-resolved before every read, so a watch follows the pointer when it moves.
- Not supported: local variables, function-static variables, and variables the compiler kept only as constants.
- Only `char` arrays show as strings; `uint8_t` and `int8_t` arrays stay numbers.
- A bit-field write reads the holding word, changes the field, writes the word back and confirms it by read-back. If the firmware changes a neighbouring field in that moment, that change is lost.
- When the ELF changes, RDT reloads it once the file has been unchanged for 0.5 s. If the rebuilt file can't be read, it keeps the old symbols.
- Parsed results are cached on disk, keyed by the ELF's hash, in `%LOCALAPPDATA%\rdt\cache` (or the folder in `RDT_CACHE_DIR`).

## SCPI profile

The SCPI profile is saved per firmware product and edited in the GUI. Example:

```toml
[scpi]
name         = "Motor controller v3"
read         = "MEM:RD? {mcu},{addr},{len}"
write        = "MEM:WR {mcu},{addr},{value},{size}"   # one value per command
value_format = "0x{value:X}"   # raw integer in hex; floats sent as their bit pattern
max_write    = 4               # largest value size the write accepts, in bytes
size_query   = "MEM:MAX?"      # optional; sets max_response on connect
addr_format  = "0x{addr:08X}"
data_format  = "hex"           # reply format of the read: hex | bytes_csv | decimal | ieee_block
byte_order   = "little"
max_response = 1024            # bytes of data the firmware can put in one reply
error_query  = "SYST:ERR?"     # sent once after a timeout, to learn the cause
terminator   = "\n"
timeout_ms   = 500
retries      = 2               # host only; re-sends of a timed-out read
probe        = "*IDN?"         # host only; sent on connect and every reconnect
# reply_regex = "^VAL (?P<data>[0-9A-F]+)$"   # optional; group "data" holds the read data

[[ranges]]                     # extra ranges RDT may access beyond the ELF's RAM and flash
mcu   = "main"                 # the MCU this range belongs to
name  = "GPIOA"                # peripheral ranges are never block-merged
start = 0x48000000
size  = 0x400
```

- **Reads:** on each poll cycle, the watched items are sorted by address and nearby items are merged into blocks no larger than `max_response`. Each block is one read, and every value is decoded from it. There is no multi-address read command.
  - Items in a peripheral range are never merged, because reading a register can clear a flag or pop a FIFO. Each is read on its own at its exact size.
  - Addresses outside the ELF's RAM and flash and the profile's `ranges` are refused before anything is sent. An access that touches a listed range must stay inside it.
- **Writes:** writes to read-only ELF sections (`.text`, `.rodata`) are refused. The value is sent as the raw integer in hex, so a float like 1.5 becomes `0x3FC00000` and the firmware never parses floats.
  - Struct and array edits become one write per field or cell. These are not atomic, and the GUI says so.
  - Values wider than `max_write` are split into pieces.
  - A write sends no reply whether it succeeds or fails, so RDT reads the value back after every write to confirm it.
- **Errors:** a failed command sends no reply at all; its error goes only to the error queue. RDT sees a timeout, sends the error query once, and shows the cause if it finds one. If the error query gets no reply either, the device counts as not answering: the link is marked down and RDT reconnects.
- **Replies:** a reply format that the fixed options cannot describe can be matched with `reply_regex`, whose group named `data` holds the read data.
- **UDP:** a timed-out read is re-sent. A timed-out write is never blindly repeated; RDT first confirms it by reading the value back.
- **Link loss:** values grey out, writes are refused, and RDT retries in the background with a growing delay. Once the link is back, it re-reads every watched value, marks the gap in the chart and the CSV log, and resumes polling.

### Sharing the link with other tools

Other tools may talk to the firmware's SCPI port over LAN while RDT is connected. A COM port can be opened by only one program, so this applies to LAN only.

- Each tool uses its own TCP connection, and the firmware must send each reply on the connection that asked. Over UDP it replies to the sender's address and port.
- Every read reply must match the requested size. A reply that doesn't fit is discarded, the input is flushed, and the read is sent again.
- A command that times out is retried with a growing delay instead of failing at once.
- The SCPI error queue is shared by all clients, so another tool can read RDT's error first, or RDT can read another tool's. RDT never depends on it: writes are confirmed by reading the value back, and an error found after a timeout is only a hint.

### Recommended firmware commands (to be added later)

```
MEMory:READ?  <mcu>,<addr>,<len>          -> hex bytes, lowest address first, e.g. 0000A041
MEMory:WRITE  <mcu>,<addr>,<value>,<size> ; one value of 1, 2 or 4 bytes
MEMory:MAX?                         -> largest <len> the read accepts (optional)
SYSTem:ERRor?                       -> 0,"No error" or a code and text
```

A failed command sends no reply and puts its error in the queue. A read larger than the firmware's buffer should fail that way, never with a truncated reply. A secondary MCU that doesn't answer should do the same with a defined error, for example `-240,"MCU not responding"`.

## Multiple MCUs

The device has several MCUs, each with its own ELF. The main MCU owns the USB/LAN link and forwards requests to the others over an internal channel.

| Topic | Decision |
| --- | --- |
| MCU IDs | The main MCU is MCU 0; the others are 1, 2, 3. Each has a display name and its own ELF in the workspace. |
| SCPI commands | The MCU is an extra parameter: `read mcu,startAddress,size` and `write mcu,address,value,size`. Templates use `{mcu}`. |
| Internal channel | MCUs 1 and up use fixed 64-byte command and response frames. The main MCU's firmware splits a large read into 64-byte pieces, so `max_response` stays one profile setting. |
| Timeouts | One profile timeout, which each MCU can override, because split reads take longer. |
| Silent MCU | The main MCU sends no reply and puts a defined error in the queue. RDT greys out only that MCU's values, keeps polling the others, and retries it in the background. Because another LAN client may read that error first, RDT also greys out an MCU after three timeouts in a row while MCU 0 still answers. |
| Watch list and chart | One mixed watch tree with an MCU column and filter. The chart and the CSV log can combine values from any MCUs; timestamps come from the PC. |

- Symbols, memory ranges, the ELF watcher and the build check all work per MCU.
- On MCUs 1 and up, a struct or array larger than 64 bytes is read in pieces, so its fields may not come from the same moment. The variable view marks such items.

### Target file

M2 keeps the connection, the SCPI profile and the MCUs in one TOML target file, which becomes the start of the M3 `.rdtw` workspace.

```toml
[connection]
transport = "tcp"
host      = "192.168.1.50"
port      = 5025

[profile]
read  = "read {mcu},{addr},{len}"
write = "write {mcu},{addr},{value},{size}"

[[mcu]]
id   = 0
name = "main"
elf  = "build/main.elf"

[[mcu]]
id         = 1
name       = "motorctl"
elf        = "build/motorctl.elf"
timeout_ms = 1500               # overrides the profile timeout for this MCU

[[mcu.ranges]]
name  = "TIM1"
start = 0x40012C00
size  = 0x400
```

A range can sit in the profile with `mcu = "name"`, or under its MCU entry; both mean the same.

### Workspace file

The `.rdtw` workspace is the target file plus the watch list, command buttons and chart layout. A plain target file opens as a workspace with no watches.

```toml
poll_ms = 500                    # global interval; 0 = off

[[watch]]
expr      = "main:motor.speed"
poll      = "global"             # global | manual | a number of ms
format    = "dec"                # dec | hex | bin | char
q         = 15                   # optional fixed-point bits
scale     = 1.0
offset    = 0.0
unit      = "rpm"
read_only = false
min       = 0                    # in shown units
max       = 3000
confirm   = true

[[button]]
name    = "Set speed"
command = "MOTOR:SPEED {param}"
param   = "rpm"                  # label for the {param} prompt

[chart]                          # kept as written, for the GUI
```

- The `[connection]`, `[profile]` and `[[mcu]]` tables are the same as in the target file.
- Shown value = raw / 2^q × scale + offset, on single values only. A write converts from shown units to the nearest raw value and is refused if it doesn't fit the type.
- Read-only on a struct or array covers all its fields. A write that needs a confirm with no one to answer it is refused.
- Saving rewrites the file, so comments in it are lost. Paths are stored relative to the file.
- CLI: `-w` opens a workspace (`-t` is the same option), `read` and `watch` default to the watch list, `write -y` skips the confirm, and `press`, `init`, `list`, `add` and `remove` run buttons and edit the watch list.

## GUI

| Panel | Purpose | Milestone |
| --- | --- | --- |
| Connection bar | COM (port, baud) or TCP/UDP (IP, port), profile selection, connect | M3 |
| SCPI profile editor | Define and test the command templates | M3 |
| SCPI console | Send any command by hand, see the raw reply, history | M3 |
| Command buttons | User-defined buttons that each send one SCPI command, with an optional parameter | M3 |
| Memory view | Hex/ASCII dump of any range, editable | M3 |
| Variable view | Debugger-style watch tree from the ELF | M3 |
| Live chart | Oscilloscope of watched values: pause, zoom, cursors | M4 |

### Variable view

- The user browses or searches the ELF's globals and adds them to the watch tree.
- Structs expand to every field and arrays to every cell, nested to any depth. Large arrays open in pages of 100 cells.
- Each value is displayed according to its type:
  - integers in decimal, hex or binary
  - floats
  - enums as `NAME (n)`
  - bools
  - bit fields extracted from their containing word
  - char arrays also as a string
  - pointers with an expandable target
- A struct or array is read as one block, so all of its fields come from the same moment. Collapsed nodes are not read at all.

### Polling interval

- A global interval applies to all watched items. The default is 500 ms, and it can be set to Off.
- Each top-level watched item (variable or raw address) can override it with Global, its own time in ms, or Manual.
- Fields and cells share their parent's interval. To poll a single field at another rate, add it as its own watched item.
- If the link can't keep up, items fall behind instead of queueing, and each row shows its achieved rate.

### v1 features adopted from FreeMASTER and STM32CubeMonitor

1. **Live chart** with pause, zoom, pan, two cursors, multiple Y axes, and 10 minutes of history.
2. **Display formats and scaling:** dec, hex, bin or char; Q15, Q31 or Qm.n; and `raw / 2^q × scale + offset` with a unit. Writes apply the inverse scaling. See [Workspace file](#workspace-file).
3. **Enum and bit-field names** taken from DWARF.
4. **Workspace file** (`.rdtw`, TOML) holding the ELF path, connection, profile, watch list, formats, limits, chart layout and buttons.
5. **ELF rebuild detection:** when the ELF changes, RDT reloads it and re-maps watched items by name. Items whose address or type changed are flagged, and items that no longer exist are kept greyed out.
6. **CSV logging** with ISO 8601 timestamps and scaled values.
7. **Write safety:** a per-item read-only flag, min/max limits, and an optional confirm before writing.
8. **Command buttons** that each send a user-defined SCPI command.
9. **Statistics:** min, max, mean and sample count per item, with a reset.

Not in v1: multi-variable read, an on-target recorder, alarms, stimulus, computed values, a scripting API, and log replay.

## Package layout

```
rdt/
  transport/   base.py, serial.py, tcp.py, udp.py   plug-in byte movers
  protocol/    scpi.py, binary.py                   command encoding
  session.py                                        sequencing, retry, timeout
  scheduler.py                                      poll intervals, block merging
  symbols/     elf.py, types.py, expr.py            ELF/DWARF, expressions
  codec.py                                          bytes <-> typed values, scaling
  target.py                                         public API
  gui/                                              PySide6 desktop app
  cli.py                                            command line
sim/                                                simulated device
tests/
```

## Testing

- **Simulated device:** written in Python. It answers SCPI commands defined by a profile, over TCP, UDP and a virtual COM pair. Its memory is loaded from the ELF's initialized data.
- **ELF corpus:** small images built with GCC and Clang, with golden tests for address, size and type.
- **Unit tests** for the codec, the expression parser, the profile templates and the block merging.
- **GUI tests** with pytest-qt.

## Milestones

| | Milestone | Contents |
| --- | --- | --- |
| M0 | Spec and skeleton | This design, repo layout, CI |
| M1 | Simulated device | SCPI over TCP, UDP and a virtual COM port; several MCUs, each with memory loaded from its ELF; 64-byte split; failed commands send no reply |
| M2 | Host core and CLI | Transports, session, SCPI profiles, the target file with several MCUs, reconnect; CLI; a test with a second SCPI client. No GUI yet |
| M3 | First GUI and symbols | Connection bar, profile editor, console, command buttons, memory view; ELF/DWARF, types, expressions, codec, variable view, formats and scaling, write safety, workspace, ELF rebuild detection |
| M4 | Watch | Polling scheduler, live chart, statistics, CSV logging |
| M5 | Binary protocol | Faster path for a future firmware agent |
| M6 | Application release | Windows installer, user docs, firmware command spec |
| Later | Firmware integration | Memory commands in the existing firmware, hardware tests |

## Open items

- Which board to use for the first hardware test. Not needed until the firmware commands exist.
- Deferred to the firmware phase: bare metal or RTOS, and the details of the binary agent.
