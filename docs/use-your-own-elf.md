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

RDT can tell whether the firmware on the device is the build your ELF came from, and refuses writes when it is not, so a stale ELF can't write to the wrong address. It reads a few bytes from the device and compares them with the same bytes in the ELF file.

**The GNU build ID (recommended).** The linker computes a SHA-1 hash over the whole linked image, so any change to code or data gives a new one. Nothing to set in RDT; it finds the build ID in the ELF by itself.

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

**Keil MDK (armlink) and other toolchains without a build ID.** armlink writes no build ID, so give the firmware a constant that changes on every build, and tell RDT its name. [`examples/keil/make_build_id.bat`](../examples/keil/make_build_id.bat) writes a new `build_id.c` before each build:

```c
__attribute__((used)) const char fw_build_id[] = "20261014-091530-3f9a1c2e";   /* build time + random */
```

1. Copy `make_build_id.bat` into the project folder (next to the `.uvprojx`), and run it once so `build_id.c` exists.
2. Add `build_id.c` to the project (any group).
3. Options for Target > User > Before Build/Rebuild: tick Run #1 and set it to `make_build_id.bat`.
4. Options for Target > Linker > Misc controls: add `--keep=fw_build_id`, so armlink doesn't remove the unused constant.
5. Options for Target > Output: keep Debug Information ticked, so RDT finds the variable by name.
6. In RDT, set `identity = "fw_build_id"` under `[[mcu]]` (or Target > MCUs and ELF files > Identity variable), and point `elf` at the `.axf` Keil writes.

The constant lands in flash, since it is `const`. On a mismatch RDT shows both IDs, such as `device '20261014-091530-3f9a1c2e', ELF '20261014-101204-77b0d4e1'`, so you can see which build is older.

**A hand-written constant.** `const char fw_build[] = __DATE__ " " __TIME__;` also works, but it only changes when its own file is compiled again, so it misses builds where only other files changed.

**When RDT checks:**

- On Connect. The status bar says `ELF matches the firmware`, `ELF does NOT match the firmware` (in red), or `ELF not checked` when there is no build ID or identity variable to compare. The console has the detail; a mismatch shows both build IDs, as `readelf -n` prints them.
- When the link comes back after a drop, such as a reset over a native USB port, and before the next write if the link dropped meanwhile.
- When the GUI loads a rebuilt ELF.
- From **Target > Check ELF identity**. Use it after flashing over ST-LINK while the COM port stays open, since then the link never drops and RDT can't tell the firmware changed.

On a mismatch, reads still work and writes are refused until you load the right ELF or choose **Target > Allow writes despite ELF mismatch** (`--allow-mismatch` on the command line).

## Other MCUs

The same steps work for any MCU: give the simulator and RDT the same banks as `kind = "memory"` ranges and the same ELF. Each MCU of a multi-MCU device gets its own `[[mcu]]` block, ELF and banks; see [`two_mcus.toml`](../sim/examples/two_mcus.toml) and [`sim_bench.rdtw`](../examples/sim_bench.rdtw).
