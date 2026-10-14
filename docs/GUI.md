# RDT GUI

The GUI works on one workspace (`.rdtw`, see [CLI.md](CLI.md#workspace)): the connection, the SCPI profile, the MCUs and their ELF files, the watch tabs, and the chart layout. Everything you change in the window is saved back to that file.

```
python -m pip install -e .[dev]
python -m sim &                          # the simulated device
rdt-gui examples/sim_bench.rdtw          # or: python -m rdt.gui examples/sim_bench.rdtw
```

Started with no file, `rdt-gui` reopens the workspace you last opened or saved. If there is none (or that file is gone), it opens `rdt.toml` from the current folder when there is one, the same default target file as the CLI (see [Default files](CLI.md#default-files)); otherwise it starts with a new workspace.

![RDT main window connected to the simulator](images/gui-main.png)

## Connection bar

- **Transport**: `tcp` or `udp` with the device's IP address and port, or `serial` with a COM port and baud rate. The COM port list shows each port with its description, such as `COM3 - GAC serial port (COM3)`; you can also type a port name.
- **Profile**: opens the SCPI profile editor (below). The button shows the profile's name and, when it comes from a file of its own, the file.
- **Connect / Disconnect** (F5). The state next to it is green while the link is up, red while it is down and RDT is reconnecting, and grey when not connected. The memory view and sending from the console are disabled unless the link is up (the transcript, search, log and command list still work), and the connection fields can only be changed while disconnected.
- **Poll** (F7, also **Watch > Poll automatically**): untick it to turn all polling off, including items with their own interval. The interval is kept and polling picks up where it was when you tick it again. Polling is on when RDT starts; the setting is not saved in the workspace.
- The interval next to it is the global polling interval. `Off` (0) stops the global polling only; items set to their own interval still poll.
- **Poll now** (F6, also **Watch > Poll now**): read every watched item once, in every tab, including manual items, whether polling is on or off.
- **Log CSV** (Ctrl+L): log every watched value to a CSV file. See [CSV log](#csv-log).

**Target > MCUs and ELF files** sets each MCU's id, name, ELF file, timeout and identity variable. MCU 0 is the main MCU and is required.

**ELF identity.** On connect, RDT compares each MCU's identity variable (a constant that changes with every build, such as a version string; or the GNU build ID when the linker put it in memory) on the device with its ELF file, and notes the result in the console. When they differ, the status bar says so in red and every write to that MCU is refused, so a stale ELF can't write to the wrong address. **Target > Allow writes despite ELF mismatch** lets writes through until the next check. **Target > Check ELF identity** checks again, such as after flashing; it also runs after an ELF is rebuilt.

## Variable view

The **Variables** panel lists an MCU's globals from its ELF. Type in the search box to filter (`motor*`, `*count`), then double-click a variable or select several and press **Add to watch list**; they go to the watch tab in front. **Watch > Add expression** (Ctrl+E) takes any expression the CLI takes, such as `main:motor.speed`, `main:gains[2]`, `main:*ptr` or `@main:0x20000000:u32`.

### Watch tabs

The centre of the window holds the watch tabs. Each tab is its own watch list with its own tree, such as one for the motor and one for the system state, and the workspace saves them all. A new workspace starts with one tab, `Watch`.

- **New tab**: the **+** at the right of the tabs, a double-click on the empty part of the tab bar, or **Watch > New tab...** (Ctrl+Shift+T).
- **Rename**: double-click a tab, or right-click it > **Rename...**. Tab names are unique, whatever the case.
- **Remove**: right-click a tab > **Remove**, or **Watch > Remove tab**. It asks first when the tab has items. The last tab can't be removed.
- **Reorder**: drag a tab along the tab bar. Ctrl+PgDown and Ctrl+PgUp go to the next and previous tab.
- **The same item in several tabs**: right-click an item > **Also show in tab**. It is still one item: its format, scaling, polling and write safety are the same in every tab, and changing them in one tab changes them in all. **Remove from this tab** takes it out of the tab in front only.

**Every tab keeps polling**, not only the one in front, so the chart and the CSV log keep getting values from items in the other tabs. One poll reads what every tab has due, and an item in several tabs is read once.

### Watch tree

The watch tree of each tab shows each watched item with its value, type, address and the rate it is actually read at:

- Structs and arrays open to every field and cell. A struct is read in one block. **A collapsed struct or array is not read**, so close what you aren't looking at to keep the link free. Arrays show 100 cells at a time; right-click and choose **Show 100 more cells** for the next page.
- Enums show as `NAME (n)`, bit fields are extracted, `char` arrays show as strings, and pointers show their target's name. Right-click a pointer and choose **Watch what it points to** to follow it.
- Right-click a field or cell and choose **Watch this on its own** to give it its own row, with its own format and polling.
- Grey values are read-only (in the workspace, or not a single value). A red value is an error; hover over it for the reason. When the link goes down or an MCU stops answering, the item shows `link down` or `MCU silent` in grey and its last values stay.

Polling: each item is read at the global interval, at its own interval, or only by hand. Everything that is due is read in one poll, and items close to each other on the same MCU share one read (see [CLI.md](CLI.md#commands)), so they come from the same moment. While a poll is still running no new one starts: an item that can't keep up falls behind rather than queueing reads, and the **Rate** column shows what it actually gets. **Poll now** (F6) reads everything once, and every item is read once on connect.

### Statistics

**View > Statistics columns** (Ctrl+T) shows **Min**, **Max**, **Mean** and **Samples** for every numeric value in the tree, fields and cells included, in shown units. They count every value read since connecting or the last reset. **Watch > Reset statistics** starts them all again, in every tab; right-click an item and choose **Reset statistics** for that item alone. Changing an item's scaling resets its statistics.

### Writing

Double-click a value (or right-click > **Write...**), type the new value, and press Enter. Type it in the item's units: with `scale = 0.001` and unit `A`, typing `1.25` writes 1250. Enums take their names (`MODE_RUN`), and `char` arrays take a string. Every write is confirmed by reading the value back.

The workspace's write safety applies: read-only items can't be edited, values outside `min`..`max` are refused with a message in the status bar, and items with `confirm` ask first. A struct marked read-only or confirm makes its fields read-only or confirm-first too. **Watch > All items read-only** makes every item read-only unless its settings say Writable, and also refuses edits in the memory view.

### Item settings

Right-click a top-level item for **Format** (dec, hex, bin, char) and **Settings...**, which sets the fixed-point format (`Q15`, `Q31`, `Q3.12`), scale, offset and unit (shown = raw / 2^q × scale + offset), polling (global, manual or a time in ms), and the write safety (read-only: Workspace default, Read-only or Writable; minimum, maximum, ask before writing).

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

**Log CSV** in the toolbar, or **Watch > Log to CSV...** (Ctrl+L), asks for a file (by default `rdt-<date>-<time>.csv` next to the workspace) and writes one row per poll until you press it again. The columns are every value the watch tabs show at that moment, in all tabs, struct fields and array cells included; while logging, collapsed structs and arrays are read too. Items added later are not logged until the log is started again. Starting the log reads everything once, so the first full row has every column. The status bar shows the file and the number of rows.

The format is the same as `rdt watch --csv`; see [CSV log](CLI.md#csv-log). A lost link and its return are marked in the `event` column, and so is an ELF rebuild.

## Memory view

Choose the MCU, type an address (`0x20000000`) or a variable name (`motor`), a length, and press **Read**. The dump shows 16 bytes per row with their ASCII. Right-click the dump to choose how the cells show memory: **Unsigned** or **Signed** with a C type (**Char** 8 bits, **Short** 16, **Int** and **Long** 32, as on ARM), in hex, or in decimal when **Decimal** is ticked; **Ascii** (one character per byte); **Float** (32-bit); or **Double** (64-bit). The current choice has a check mark, values use the MCU's byte order, and the choice is remembered for the next start. The default is unsigned Char in hex, the plain hex bytes. Double-click a cell, type a new value in the shown format (a hex `0x` prefix is accepted in decimal too), and press Enter to write it; a value too big for the type is refused. **Refresh** reads again at the global polling interval. Access outside the ELF's memory and the target's ranges is refused, as in the CLI.

## SCPI console

The console is a dark terminal. Each line shows its time, a tag (`TX` sent, `RX` reply, `ERR` error, `SYS` RDT's own notes) and the text. The last line is the prompt: the link's address (such as `COM3`), green while connected, then `>`. Type any SCPI command there, on the console's next free line, and press Enter; the command becomes a `TX` line and the prompt moves down. There is no separate input line or Send button. Replies that arrive while you type appear above the prompt without disturbing it. Because the firmware sends nothing back when a command fails, a query with no reply shows `(no reply; error queue: ...)` with what `SYST:ERR?` said. To read the queue yourself, type `SYST:ERR?`.

- **History.** Every command you send is saved, per user, in `%LOCALAPPDATA%\rdt\console_history.txt` (the last 500, a repeated command moved to the end). The GUI and `rdt console` share it, and it is kept across launches. Up and Down walk through it; going past the newest gives back what you were typing. **History** shows it newest first beside the transcript, like **Commands**; with both open they sit side by side. Double-click or **Send** sends an entry again, **Delete** removes one, **Clear all** empties it.
- **Known commands.** **Commands** shows the workspace's list of the device's commands beside the transcript, by group, with a filter box; a command's description is its tooltip. Double-click one to put it in the input line, fill in any values, and press Enter. **Import...** reads a CSV file (columns `command`, `name`, `group`, `description`, separated by commas, semicolons or tabs) or a text file with one command per line, and asks whether to add to the list (skipping commands already in it) or replace it. **Export...** writes the list as CSV, and **Clear** empties it. The list is saved in the workspace and works while disconnected.
- **Suggestions while typing.** As you type, a list of the matching commands opens under the cursor at once: your history (newest first), then the imported known commands, those that start with what you typed before those that only contain it. The best match also shows in grey after the cursor; → takes it. In the list, ↑ and ↓ move the highlight, Tab or Enter takes the highlighted command, and Tab with nothing highlighted takes the first. Enter with nothing highlighted sends what you typed, and Esc closes the list.
- **Tab completion.** With the list closed, Tab (or Ctrl+Space) completes what you typed: a single match is filled in at once; with several, the part they share is filled in and the list opens.
- **Transcript.** Each line starts with its time to the millisecond (**Timestamps** hides it). Commands sent are green, replies light, errors (no reply, link down) red, and RDT's notes (connected, link down, reconnecting, sent with no reply expected) grey italic. A reply of several lines lines up under its first line.
- **Search.** Ctrl+F opens a find bar under the transcript. Every match is highlighted, the counter shows which one you are on (`3 of 12`), Enter and Shift+Enter go to the next and previous match, and Esc closes it. Ctrl+K clears the transcript, like **Clear**.
- **Text size and wrap.** Ctrl+= and Ctrl+- (or Ctrl+mouse wheel over the transcript) make the console text larger or smaller, and Ctrl+0 resets it. **Wrap** wraps long lines; off (the default), they scroll sideways. Both are remembered for next time.
- **Send break.** On a COM port link, the transcript's right-click menu has **Send break (250 ms)**, for firmware that resets its parser on a break.
- **Hex and text.** Select part of the transcript and right-click: **Selection as hex** shows its bytes (`Hi` is `48 69`), and **Hex selection as text** turns hex such as `48656C6C6F`, `48 65 6C` or `0x48,0x65` back into text (`Hello`), with any non-printable byte as `\xNN`. The result opens in a small window with **Copy**.
- **Log to file.** Asks for a text file and appends every console line to it, with its date and time and a mark: `>` sent, `<` reply, `!` error, `#` note. Click again to stop. The file is appended to, never overwritten. This is separate from the CSV value log.

## Keyboard shortcuts

**Help > Keyboard shortcuts** (F1) lists every shortcut in the menus and the console in one table.

## SCPI profile editor

The profile says how the firmware spells a memory read and write and how it encodes the reply. The editor has every field of the `[scpi]` table (see [CLI.md](CLI.md#scpi-profile)), shows the read and write commands as they will be sent, and refuses a template that would not work (for example a read without `{len}`). While connected, **Test read** sends one read with the edited profile and shows the reply and the decoded bytes, before you apply it.

Pressing OK stores the edited profile inside the workspace (no longer pointing at the profile file) and reconnects. **Target > Load profile file...** uses a profile file instead.

## Files

**File > New, Open, Save, Save as**. Opening a plain target file (`.toml`) starts a new workspace from it, so Save asks where to put the `.rdtw` rather than overwriting the target file. A workspace from before watch tabs opens with its watch list in one tab, `Watch`. Command buttons are no longer part of RDT: a workspace that has them still opens, the status bar says they were dropped, and they are left out when it is saved. Type such commands in the SCPI console instead. The title shows `*` while there are unsaved changes, and closing the window asks whether to save them.
