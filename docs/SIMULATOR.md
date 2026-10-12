# Simulated device

The simulator stands in for the firmware until the memory commands exist there (milestone M1). It is a separate Python program that runs on the same PC as RDT and answers SCPI over TCP, UDP and a serial port. It simulates several MCUs behind one main MCU, as in [Multiple MCUs](DESIGN_DOCUMENT.md#44-multiple-mcus).

## Run it

```
python -m pip install -e .
python -m sim -v
```

With no arguments it loads [`sim/examples/two_mcus.toml`](../sim/examples/two_mcus.toml): MCU 0 `main` and MCU 1 `motorctl`, each loaded from its own ELF, listening on TCP and UDP port 5025 on `127.0.0.1`. `-vv` logs every command and reply.

| Option | Meaning |
| --- | --- |
| `--config FILE` | Simulator config (TOML) |
| `--tcp HOST:PORT`, `--udp HOST:PORT` | Where to listen, or `off` |
| `--serial PORT` | Serve a serial port too: `COM11` on Windows, `pty` on Linux, or `off` |
| `--baud N` | Serial baud rate |
| `--udp-drop F` | Drop this fraction (0 to 1) of UDP requests, to exercise retries |

To try it by hand, open a raw TCP session to `127.0.0.1:5025` (PuTTY "Raw", or `ncat`) and type `*IDN?`.

### Serial port on Windows

Windows has no built-in virtual COM pair, so install [com0com](https://com0com.sourceforge.net/), which creates two linked ports such as `COM11` and `COM12`. Run `python -m sim --serial COM11` and point RDT (or a terminal) at `COM12`. On Linux, `--serial pty` creates a pseudo-terminal and prints its path.

## Commands

The read, write, size and error commands come from the SCPI profile, the same `[scpi]` table the host uses (see [`default_profile.toml`](../sim/examples/default_profile.toml)). The simulator answers whatever templates the profile defines, so a different command set can be tried before the firmware has it.

| Command (default profile) | Reply |
| --- | --- |
| `MEMory:READ? <mcu>,<addr>,<len>` | `<len>` bytes, lowest address first, in the profile's `data_format` |
| `MEMory:WRITE <mcu>,<addr>,<value>,<size>` | none. `<size>` is 1, 2, 4 or 8 bytes, at most `max_write` |
| `SYSTem:ERRor?` | oldest error, or `0,"No error"` |
| `*IDN?`, `*RST`, `*CLS`, `*OPC?` | standard. `*RST` reloads every MCU's memory from its image |
| `SIMulation:MCU:SILent <mcu>,ON\|OFF` | none. Makes a secondary MCU stop answering |
| `SIMulation:MCU:SILent? <mcu>` | `1` or `0` |
| `SIMulation:MCU:LIST?` | `0,"main",1,"motorctl"` |

Long and short header forms both work, in any case (`MEMORY:READ?`, `mem:read?`). Numbers are decimal, `0x..`, or SCPI `#H..`. If the profile's `addr_format` or `value_format` is bare hex such as `{value:X}`, that field is parsed as hex.

Read reply formats (`data_format`):

| Format | 4 bytes `00 00 C0 3F` |
| --- | --- |
| `hex` | `0000C03F` |
| `bytes_csv` | `00,00,C0,3F` |
| `decimal` | `0,0,192,63` |
| `ieee_block` | `#14` followed by the 4 raw bytes |

## Errors

As in the firmware, a failed command sends no reply at all, so a failed read ends in a timeout. The error goes to the SCPI error queue, which all clients share, and `SYSTem:ERRor?` reads it. For experiments, `error_reply = true` makes a failed query reply at once with its error text instead. Error replies start with `-` and data replies never do, so the two can't be confused.

| Error | When |
| --- | --- |
| `-102,"Syntax error"` | Known header, wrong parameters |
| `-104,"Data type error"` | A parameter isn't a number |
| `-113,"Undefined header"` | Unknown command |
| `-222,"Data out of range"` | Address not mapped, write to read-only memory, or value too wide for its size |
| `-223,"Too much data"` | Read longer than `max_response` |
| `-224,"Illegal parameter value"` | Unknown MCU, bad length or write size |
| `-240,"MCU not responding"` | A secondary MCU is silent. The main MCU waits `internal_timeout_ms`, then queues this error |
| `-350,"Queue overflow"` | More than 16 errors queued |

## Config

```toml
idn                 = "RDT,Simulator,SIM0001,0.1.0"
profile             = "default_profile.toml"   # or an inline [scpi] table
internal_timeout_ms = 50
error_reply         = false

[transport]
tcp      = "127.0.0.1:5025"
udp      = "127.0.0.1:5025"
serial   = "COM11"
baud     = 115200
udp_drop = 0.0

[[mcu]]
id               = 1               # 0 is the main MCU, which is required
name             = "motorctl"
elf              = "firmware/motorctl.elf"
byte_order       = "little"        # default: from the ELF, else little
frame_size       = 64              # internal channel frame; reads are split into frames of this size
frame_latency_ms = 1               # time per internal frame
silent           = false

  [[mcu.ranges]]                   # memory with no ELF section behind it, such as peripherals
  name     = "GPIOA"
  start    = 0x48000000
  size     = 0x400
  writable = true
  fill     = 0

  [[mcu.signals]]                  # a variable the firmware keeps changing
  name      = "temperature"
  addr      = 0x20000088
  type      = "f32"                # u8 i8 u16 i16 u32 i32 u64 i64 f32 f64
  kind      = "sawtooth"           # counter | sine | square | sawtooth
  period_ms = 5000
  amplitude = 10
  offset    = 30
  step      = 1                    # counter only: added every period
```

How the memory behaves:
- Every allocated ELF section is loaded at its run address: `.data` with its initial values and `.bss` as zeros. Sections without the write flag, such as `.text` and `.rodata`, are read-only.
- A read may span adjacent sections. A read that touches any unmapped byte fails as a whole.
- A signal is recomputed from the time each time its memory is read, and overwrites whatever was written there.
- On a secondary MCU, signals are recomputed before each internal frame. So a struct bigger than one frame can mix values from different moments, as on the real device.
- All transports and clients share one command handler that runs one command at a time, as the firmware would.

## Example firmware

[`sim/examples/firmware`](../sim/examples/firmware) holds the two small C programs behind the example ELFs, built for a Cortex-M4 with clang. `build.sh` rebuilds them; the ELFs are committed, so the simulator and the tests need no compiler.
