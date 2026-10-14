# Use your own ELF

The simulator and RDT both read the firmware's ELF file. The simulator uses it to build the MCU's memory, and RDT uses it to find variables by name. Point both at the ELF your build produces, and you can try RDT on your own variables before the firmware has the memory commands.

## What the ELF needs

- **Debug info.** Build with `-g` (GCC or clang; any `-O` level). Without it RDT finds no variable names or types and can only read raw addresses such as `@Interface:0x24000000:u32`. `rdt -w my.rdtw symbols Interface` lists what RDT found.
- **The image that is linked, not an object file.** Use the `.elf` (or `.axf`, `.out`) the linker writes, not a `.o`.
- Any MCU, little- or big-endian, 32- or 64-bit. RDT and the simulator take the byte order from the ELF.

## Start from the STM32H7 example

[`sim/examples/stm32h7_interface.toml`](../sim/examples/stm32h7_interface.toml) (simulator) and [`examples/stm32h7_interface.rdtw`](../examples/stm32h7_interface.rdtw) (RDT workspace) describe one MCU named `Interface` with the STM32H743/H753 memory map:

| Bank | Start | End | Size | |
| --- | --- | --- | --- | --- |
| ITCM | `0x00000000` | `0x0000FFFF` | 64 KB | |
| FLASH | `0x08000000` | `0x081FFFFF` | 2 MB | read-only; erased (`0xFF`) in the simulator |
| DTCM | `0x20000000` | `0x2001FFFF` | 128 KB | |
| AXI_SRAM | `0x24000000` | `0x2407FFFF` | 512 KB | |
| SRAM1_3 | `0x30000000` | `0x30047FFF` | 288 KB | SRAM1, SRAM2 and SRAM3 |
| SRAM4 | `0x38000000` | `0x3800FFFF` | 64 KB | |
| BKPSRAM | `0x38800000` | `0x38800FFF` | 4 KB | backup SRAM |

Run them as they are, with no ELF:

```
python -m sim --config sim/examples/stm32h7_interface.toml
rdt-gui examples/stm32h7_interface.rdtw
```

RDT reads and writes anywhere in those banks and refuses everything else. The simulator keeps a counter running at `0x24000000` so something moves.

For another STM32H7 part, change the banks to match its reference manual, in both files. Flash smaller than 2 MB is the usual difference.

## Add your ELF

1. Copy both files somewhere of your own, such as next to your firmware build. If you copy the simulator config, also copy `sim/examples/default_profile.toml` next to it, or change `profile` to point at it.
2. In both files, uncomment `elf` under `[[mcu]]` and set it to your ELF:

   ```toml
   [[mcu]]
   id   = 0
   name = "Interface"
   elf  = "C:/work/firmware/build/interface.elf"
   ```

   A relative path is relative to the file it is in. In the GUI you can instead use **Target > MCUs and ELF files** and then save the workspace.
3. In the simulator config, delete the `uptime_ms` signal, or set its `addr` to one of your own variables. A signal overwrites its address every time it is read.
4. Start the simulator with your config, then RDT with your workspace, and press **Connect**. The **Variables** panel lists your globals; double-click one to watch it.

To skip `--config` and `-w` every time, save the two files as `sim.toml` and `rdt.toml` in the folder you start from (the repository root is fine, git ignores them there). `python -m sim`, `rdt` and `rdt-gui` then pick them up by themselves; see [Default config](SIMULATOR.md#default-config-simtoml) and [Default files](CLI.md#default-files).

What the simulator does with the ELF:

- Every section the firmware loads is placed at its run address, over the banks. `.data` starts with its initial values, `.bss` and the rest of RAM start as zeros, and the rest of flash reads as `0xFF`.
- `.text`, `.rodata` and other sections without the write flag stay read-only, even inside a writable bank. RDT refuses writes to them before sending anything.
- Variables keep their power-up values until you write them. Nothing in the firmware runs, so add `[[mcu.signals]]` for values you want to see change (see [SIMULATOR.md](SIMULATOR.md#config)).
- Peripheral registers are not simulated. To read or write a register block, add it as a `kind = "peripheral"` range in both files.

## When you rebuild

The RDT GUI checks the ELF every second while connected and loads the new one by itself: watched variables are found again by name, and the GUI flags any that moved or changed type. The simulator loads its memory only when it starts, so restart it after a rebuild to get the new initial values. Until then it still has the old layout.

## Check that the device runs this ELF

RDT can tell whether the firmware on the device is the build your ELF came from. When it is not, RDT refuses reads and writes to that MCU, so a stale ELF can't read or write the wrong address.

**With no setup: a flash sample (any toolchain, Keil included).** The ELF holds the exact bytes of everything in flash: code, constants and the vector table. On Connect, RDT reads the start and end of each read-only section plus 32 blocks of 64 bytes spread over them (about 4 KB for a large image) and compares them with the ELF. Code and constants shift after almost any rebuild, so an outdated image is caught in practice; a change of a single constant could slip through. Nothing to set in the firmware, the build or RDT.

If the firmware has a build ID or an identity variable (below), RDT compares that instead, which catches every change.

**The GNU build ID (GCC and clang).** The linker computes a SHA-1 hash over the whole linked image, so any change to code or data gives a new one. Nothing to set in RDT; it finds the build ID in the ELF by itself.

1. Link with `-Wl,--build-id`. In STM32CubeIDE: Project > Properties > C/C++ Build > Settings > MCU GCC Linker > Miscellaneous > Other flags.
2. Keep the build ID in flash. In the linker script (`STM32H7xx_FLASH.ld`), add this section right after `.isr_vector`:

   ```
     .note.gnu.build-id :
     {
       KEEP(*(.note.gnu.build-id))
     } >FLASH
   ```

   Keep the section name exactly as it is. Without the section, the linker places the note where it likes; it can land at the start of flash, in front of the vector table.
3. Rebuild and flash. `arm-none-eabi-readelf -n build/interface.elf` prints `Build ID: ...`, and `arm-none-eabi-readelf -S` lists `.note.gnu.build-id` at a flash address with flag `A`.

[`link_build_id.ld`](../sim/examples/firmware/link_build_id.ld) is a small linker script laid out this way.

**A constant variable instead.** Set `identity = "fw_build"` under `[[mcu]]`, or the Identity variable column in Target > MCUs and ELF files, and give the firmware a constant that changes with every build, such as `__attribute__((used)) const char fw_build[] = __DATE__ " " __TIME__;`. It must be `const` (in flash), and it only changes when its own file is compiled again, so the build ID is the safer choice.

**When RDT checks:**

- On Connect. The toolbar's image box, next to the connection status, shows `Image ✓ verified` (green), `Image ✗ mismatch` (red), `Image: checking`, `Image ? not read` (the MCU did not answer) or `Image not checked` (nothing to compare); its tooltip has each MCU's result. The status bar and the console say the same, with the detail: the first flash address that differs, or both build IDs.
- When the link comes back after a drop, such as a reset over a native USB port, and before the next write if the link dropped meanwhile.
- When the GUI loads a rebuilt ELF.
- From **Target > Check ELF identity**. Use it after flashing over ST-LINK while the COM port stays open, since then the link never drops and RDT can't tell the firmware changed.

On a mismatch, every read and write to that MCU is refused, including polling and the memory view, until you load the matching ELF (or flash the matching firmware) and connect or check again. The GUI has no way around it; on the command line, `--allow-mismatch` lets a single command through. The console still sends commands you type.

## Other MCUs

The same steps work for any MCU: give the simulator and RDT the same banks as `kind = "memory"` ranges and the same ELF. Each MCU of a multi-MCU device gets its own `[[mcu]]` block, ELF and banks; see [`two_mcus.toml`](../sim/examples/two_mcus.toml) and [`sim_bench.rdtw`](../examples/sim_bench.rdtw).
