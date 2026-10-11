# RDT GUI

The GUI works on one workspace (`.rdtw`, see [CLI.md](CLI.md#workspace)): the connection, the SCPI profile, the MCUs and their ELF files, the watch list, and the command buttons. Everything you change in the window is saved back to that file.

```
python -m pip install -e .[dev]
python -m sim &                          # the simulated device
rdt-gui examples/sim_bench.rdtw          # or: python -m rdt.gui examples/sim_bench.rdtw
```

![RDT main window connected to the simulator](images/gui-main.png)

## Connection bar

- **Transport**: `tcp` or `udp` with the device's IP address and port, or `serial` with a COM port and baud rate.
- **Profile**: opens the SCPI profile editor (below). The button shows the profile's name and, when it comes from a file of its own, the file.
- **Connect / Disconnect** (F5). The state next to it is green while the link is up, red while it is down and RDT is reconnecting, and grey when not connected. The console, memory view and buttons are disabled unless the link is up, and the connection fields can only be changed while disconnected.
- **Poll**: the global polling interval. `Off` (0) stops polling; items set to their own interval still poll.
- **Log CSV** (Ctrl+L): log every watched value to a CSV file. See [CSV log](#csv-log).

**Target > MCUs and ELF files** sets each MCU's id, name, ELF file and timeout. MCU 0 is the main MCU and is required.

## Variable view

The **Variables** panel lists an MCU's globals from its ELF. Type in the search box to filter (`motor*`, `*count`), then double-click a variable or select several and press **Add to watch list**. **Watch > Add expression** (Ctrl+E) takes any expression the CLI takes, such as `main:motor.speed`, `main:gains[2]`, `main:*ptr` or `@main:0x20000000:u32`.

The watch tree in the centre shows each watched item with its value, type, address and the rate it is actually read at:

- Structs and arrays open to every field and cell. A struct is read in one block. **A collapsed struct or array is not read**, so close what you aren't looking at to keep the link free. Arrays show 100 cells at a time; right-click and choose **Show 100 more cells** for the next page.
- Enums show as `NAME (n)`, bit fields are extracted, `char` arrays show as strings, and pointers show their target's name. Right-click a pointer and choose **Watch what it points to** to follow it.
- Right-click a field or cell and choose **Watch this on its own** to give it its own row, with its own format and polling.
- Grey values are read-only (in the workspace, or not a single value). A red value is an error; hover over it for the reason. When the link goes down or an MCU stops answering, the item shows `link down` or `MCU silent` in grey and its last values stay.

Polling: each item is read at the global interval, at its own interval, or only by hand. Everything that is due is read in one poll, and items close to each other on the same MCU share one read (see [CLI.md](CLI.md#commands)), so they come from the same moment. While a poll is still running no new one starts: an item that can't keep up falls behind rather than queueing reads, and the **Rate** column shows what it actually gets. **Watch > Read all now** (F6) reads everything once, and every item is read once on connect.

### Statistics

**View > Statistics columns** (Ctrl+T) shows **Min**, **Max**, **Mean** and **Samples** for every numeric value in the tree, fields and cells included, in shown units. They count every value read since connecting or the last reset. **Watch > Reset statistics** starts them all again; right-click an item and choose **Reset statistics** for that item alone. Changing an item's scaling resets its statistics.

### Writing

Double-click a value (or right-click > **Write...**), type the new value, and press Enter. Type it in the item's units: with `scale = 0.001` and unit `A`, typing `1.25` writes 1250. Enums take their names (`MODE_RUN`), and `char` arrays take a string. Every write is confirmed by reading the value back.

The workspace's write safety applies: read-only items can't be edited, values outside `min`..`max` are refused with a message in the status bar, and items with `confirm` ask first. A struct marked read-only or confirm makes its fields read-only or confirm-first too.

### Item settings

Right-click a top-level item for **Format** (dec, hex, bin, char) and **Settings...**, which sets the fixed-point format (`Q15`, `Q31`, `Q3.12`), scale, offset and unit (shown = raw / 2^q × scale + offset), polling (global, manual or a time in ms), and the write safety (read-only, minimum, maximum, ask before writing).

### ELF rebuilds

RDT checks the ELF files every second while connected. When one is rebuilt, its MCU's watched items are found again by name: an item that moved or changed type is shown in orange with a note in its tooltip and the status bar, and an item that no longer exists shows `gone` in grey. The variables list reloads too.

## Live chart

![The live chart with two signals on two axes and the cursors on](images/gui-chart.png)

The **Chart** panel plots watched values over time. Right-click a number, bool or enum in the watch tree (a variable, a field or a cell) and choose **Add to chart**, or select several and press Ctrl+G. Values from any MCUs can share the chart; the time is the PC's clock when each reply arrived.

- **Following.** The chart shows the newest data over the **Window** (1 s to 10 min). Each signal keeps the last 10 minutes.
- **Pause** (or Space) freezes the view while data keeps coming in. Dragging or zooming with the mouse pauses too. **Run** goes back to following.
- **Zoom and pan.** The mouse wheel zooms, dragging pans, and right-dragging zooms each axis on its own. The wheel over a Y axis zooms only that axis. **Fit Y** scales every Y axis to the data in view again.
- **Y axes.** There are up to four: axis 1 on the left, the others on the right. Signals with the same unit share an axis by default, and each axis is labelled with its units. Right-click a signal in the list to move it to another axis, change its colour or remove it; untick it to hide it.
- **Cursors** shows two vertical cursors. Drag them; the list shows each signal's value at C1 and C2 and the difference, and the bar above shows both times, the time between them and its frequency.
- **Gaps.** When the link goes down or a value can't be read, its line breaks rather than joining across the gap.
- **Clear** drops the recorded data and keeps the signals.

A charted field of a struct or array is read even while its row is collapsed. The chart's window and signals, with their axes and colours, are saved in the workspace's `[chart]` table:

```toml
[chart]
window_s = 10

[[chart.series]]
expr  = "main:motor.speed"
axis  = 1
color = "#1f77b4"
unit  = "rpm"
```

## CSV log

**Log CSV** in the toolbar, or **Watch > Log to CSV...** (Ctrl+L), asks for a file (by default `rdt-<date>-<time>.csv` next to the workspace) and writes one row per poll until you press it again. The columns are every value the watch tree shows at that moment, struct fields and array cells included; while logging, collapsed structs and arrays are read too. Items added later are not logged until the log is started again. Starting the log reads everything once, so the first full row has every column. The status bar shows the file and the number of rows.

The format is the same as `rdt watch --csv`; see [CSV log](CLI.md#csv-log). A lost link and its return are marked in the `event` column, and so is an ELF rebuild.

## Memory view

Choose the MCU, type an address (`0x20000000`) or a variable name (`motor`), a length, and press **Read**. The dump shows 16 bytes per row with their ASCII. Double-click a byte, type two hex digits, and press Enter to write it. **Refresh** reads again at the global polling interval. Access outside the ELF's memory and the target's ranges is refused, as in the CLI.

## SCPI console

Type any SCPI command and press Enter. Up and Down walk through earlier commands. Because the firmware sends nothing back when a command fails, a query with no reply shows `(no reply; error queue: ...)` with what `SYST:ERR?` said. **Error queue** reads `SYST:ERR?` until the queue is empty.

## Command buttons

The **Buttons** panel holds the workspace's buttons. Pressing one sends its command through the console. A command with `{param}` asks for a value first. **Add button...** makes a new one; right-click a button to edit or remove it.

## SCPI profile editor

The profile says how the firmware spells a memory read and write and how it encodes the reply. The editor has every field of the `[scpi]` table (see [CLI.md](CLI.md#scpi-profile)), shows the read and write commands as they will be sent, and refuses a template that would not work (for example a read without `{len}`). While connected, **Test read** sends one read with the edited profile and shows the reply and the decoded bytes, before you apply it.

Pressing OK stores the edited profile inside the workspace (no longer pointing at the profile file) and reconnects. **Target > Load profile file...** uses a profile file instead.

## Files

**File > New, Open, Save, Save as**. Opening a plain target file (`.toml`) starts a new workspace from it, so Save asks where to put the `.rdtw` rather than overwriting the target file. The title shows `*` while there are unsaved changes, and closing the window asks whether to save them.
