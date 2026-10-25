# KiCad schematic rules

General requirements for schematics drawn with this MCP server. They apply
to every project unless the project asks otherwise. Choices for a single
board belong in that project's own notes. PCB layout rules are in
[PCB_RULES.md](PCB_RULES.md).

These rules are also sent to every MCP client in the server's instructions
(`server.py`), and `verify_schematic` checks the ones it can.

## General

- Don't add, remove or change any part, value or net from the source data
  (CSV, netlist, datasheet). If something can't be mapped, stop and ask.
- Check that each footprint's pad numbers match its symbol's pin numbers.
- Commit and push only when asked.

## Sheet and grid

- Every connection point (pin end, wire end, label, junction) is on the
  100 mil (2.54 mm) grid. The tools enforce it.
- Everything stays inside the drawing frame and clear of the title block.
- One folder per project.
- The top (root) sheet always carries a short project description at the
  bottom, in its own block ("About this project"): what the board does, its
  main parts and interfaces, and where the source data comes from, in a few
  plain lines.

## Functional blocks

- Each functional block (e.g. "USB PD controller", "Level shifter",
  "3V3 DC/DC") sits in a dashed rectangle with its title in the top-left
  corner (`add_block`).
- Blocks must not overlap. Each block's symbols, wires and text stay inside
  it, below the title.
- Related blocks sit next to each other, e.g. a regulator directly below the
  circuit it feeds.
- Blocks tile the sheet with one equal gap: between neighbouring blocks and
  between blocks and the drawing frame or title block (`get_sheet_layout`).
  Each circuit is centred in its block.
- Blocks connect by net labels and power symbols, never by wires crossing a
  block border.

## Symbols

- Resistors: `Device:R_Small`. Capacitors: `Device:C_Small`.
- Value field: the short value only (10µF, 5.1kΩ, 5mΩ). No descriptions
  such as "TVS, 22 V standoff, 28.35 V clamp" or "N-MOSFET, 50 V". The full
  source text goes in the hidden Description field.
- ICs, MOSFETs, TVS diodes and LEDs: the part number is the value, placed
  next to the part like any value.
- Secondary information worth seeing goes in its own field, shown on the
  line below the value, smaller than the value and grey. For example a
  capacitor's voltage, a resistor's tolerance or power, an inductor's
  saturation current.
- Capacitors: Value = capacitance, `Voltage` field shown, `Dielectric`
  hidden.
- Supply rails use power symbols, including connector rails such as VBUS. A
  rail without its own library symbol uses a similar one with the net as its
  value.
- GND symbols face down with the "GND" text hidden. Supply symbols face up.
- Power-symbol references are hidden.
- Each power symbol (+3V3, +1V8, ...) sits at the end of its rail, directly
  above the component it feeds (the supply pin, or the outermost capacitor on
  a horizontal rail), never in the middle of the rail.
- Rotate or mirror symbols where that gives a cleaner layout, e.g. a
  back-to-back MOSFET pair.
- Symbols created or redrawn for a project: no signal pins on the bottom.
  Inputs on the left, outputs on the right; supply pins on top, ground pins
  on the bottom. Power-path parts (regulators, load switches) keep their
  main input on the left and output on the right.

## Text

- Reference and value sit on two separate lines, Reference above Value, in
  the same position relative to every symbol (the side without pins).
- Text must not overlap wires, symbols or other text.
- Text size follows the project's default text size (Schematic Setup).
- KiCad treats a symbol as one rectangle around its body, pins and text.
  Keep other symbols out of that rectangle, or they print as overlapping.

## Wiring and labels

- Wire each supporting part (resistor, capacitor, LED, diode, transistor)
  to the IC pin it serves, and put the net label on that wire.
- Any passive that belongs to one pin or one function sits next to that pin
  and is wired to it: decoupling and bulk capacitors, crystal and load
  capacitors, series and termination resistors, AC-coupling capacitors,
  reference capacitors, current-limit and feedback resistors, ESD diodes at a
  connector. This holds even when the source data gives every net a name
  ("connect each pin with a net label"): the label goes on the wire, it does
  not replace the wire. Labels alone only join functional blocks or ICs.
- Decoupling capacitors sit vertically, directly at the pin they serve, each
  with its own ground symbol.
- Group decoupling capacitors per power pin: all capacitors of one supply pin
  (e.g. DVDDIO, DVDD, MVDD, PVDD, AVDD) sit together at that pin, the
  smallest value closest to it. When the source data doesn't say which
  capacitor serves which pin, assign them in order and state the assignment.
- Pull-ups, pull-downs and gate resistors sit next to the part they belong
  to.
- No wire run is longer than a quarter of the sheet width (A4: 74 mm, A3:
  105 mm). Longer connections get a net label at both ends instead.
- Net names: when the user gives a source for them (a connection CSV, a
  netlist, a reference design, ...) or says which to use, those names are
  used exactly. Only when no source is given, nets are named by function and
  prefixed by their block, e.g. PD_SCL, PD_SDA, PD_OTP.
- Wires connected to power symbols (power ports) are red
  (`color_power_wires`); ground wires keep the default colour.
- Test points sit in the circuit, on the wire of the net they probe, next to
  the part concerned (e.g. VBUS at the sense resistor, VOUT, 5V, 1V8). No
  lone test points.
- A label's text lies along its own wire: the wire is at least as long as
  the text, and at a stub's free end the label points back towards the pin.
- Wires may cross, but a crossing never joins two nets.
- Keep it compact: short wires, few bends, parts close to their pins.
  Keep rails short: a supply rail runs from the pin past its capacitors and
  ends at its power symbol, with no empty wire beyond the last part.
- Parts with many ground pins (e.g. an FPC connector with GND between the
  signal pairs): join the ground pins with short stubs to one vertical bus
  wire, with a junction at each stub and a single GND symbol at the bus
  end, instead of a GND symbol per pin. Signal stubs may cross the bus.

## No-connects and ERC

- Don't flag pins that the library already hides as no-connect. Flag only
  visible unused pins.
- Add a PWR_FLAG to every supply and ground net that no power-output pin
  drives, such as power arriving through a connector.
- PWR_FLAGs never sit next to parts. They go in their own block ("Power
  flags (ERC)"): one row per rail, with the power symbol at one end of a
  short wire, the PWR_FLAG on it and a global label with the rail's name at
  the other end. In a hierarchical design that block is on the top (parent)
  sheet, with the circuit on the child sheets. The global label is needed:
  without it KiCad 10's ERC reports "Pin not connected" for a power symbol
  and PWR_FLAG on a page without real pins.
- Finish with `verify_schematic`. It must report `ok`, and the PDF must be
  checked by eye.
- Compare the netlist with the source data.
- Report ERC results exactly as returned.
