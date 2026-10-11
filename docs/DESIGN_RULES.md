# KiCad design rules

General requirements for schematics and PCBs built with this MCP server.
They apply to every project unless the project asks otherwise. Choices for a
single board (footprints, fab, ERC exceptions) belong in that project's own
notes.

These rules are also sent to every MCP client in the server's instructions
(`server.py`), and `verify_schematic` checks the ones it can.

## General

- Don't add, remove or change any part, value or net from the source data
  (CSV, netlist, datasheet). If something can't be mapped, stop and ask.
- Check that each footprint's pad numbers match its symbol's pin numbers.
- Commit and push only when asked.

## Schematic

### Sheet and grid

- Every connection point (pin end, wire end, label, junction) is on the
  100 mil (2.54 mm) grid. The tools enforce it.
- Everything stays inside the drawing frame and clear of the title block.
- One folder per project.

### Functional blocks

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

### Symbols

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
- Rotate or mirror symbols where that gives a cleaner layout, e.g. a
  back-to-back MOSFET pair.

### Text

- Reference and value sit on two separate lines, Reference above Value, in
  the same position relative to every symbol (the side without pins).
- Text must not overlap wires, symbols or other text.
- Text size follows the project's default text size (Schematic Setup).
- KiCad treats a symbol as one rectangle around its body, pins and text.
  Keep other symbols out of that rectangle, or they print as overlapping.

### Wiring and labels

- Wire each supporting part (resistor, capacitor, LED, diode, transistor)
  to the IC pin it serves, and put the net label on that wire.
- Decoupling capacitors sit vertically, directly at the pin they serve, each
  with its own ground symbol.
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

### No-connects and ERC

- Don't flag pins that the library already hides as no-connect. Flag only
  visible unused pins.
- Add a PWR_FLAG to every supply and ground net that no power-output pin
  drives, such as power arriving through a connector.
- Finish with `verify_schematic`. It must report `ok`, and the PDF must be
  checked by eye.
- Compare the netlist with the source data.
- Report ERC results exactly as returned.

## PCB

### Setup

- Import from the schematic. Footprints carry the schematic's fields, BOM
  flags and symbol links, so KiCad's Update PCB from Schematic matches them.
- Hide all references and values.
- 4 layers:
  - F.Cu and B.Cu carry signals and power.
  - In1.Cu and In2.Cu are GND layers (type power). No pour until asked.
- F.Fab and B.Fab are disabled.
- Set the design rules from the chosen fab's published capabilities. State
  which limits the fab doesn't publish, and which values may cost extra.

### Placement

- Connectors sit on the board edges, opening outwards.
- Group related parts.
- Decoupling capacitors and all supporting parts sit as close as possible
  to the IC pins they serve, with the connecting pad facing the pin.
- Rotate ICs so high-speed or differential pins face their connector, and
  keep the lane between them clear.
- Power paths are short and straight: connector → sense resistor → switches
  → output connector. Orient parts so their pads face along the path.
- No part of another net may sit on a power path.
- Keep signal circuits, such as level shifters, near the connector they
  serve and out of the power path.
- Put sensors such as thermistors next to the parts they monitor.
- Make the board as compact as possible within the rules above, and report
  what else would shrink it (for example moving a connector).

### Checks

- DRC with schematic parity: 0 errors. Explain every remaining warning.
- Render the board and look at it.
- Report key distances: decoupling pad to pin, power path lengths and
  differential pair lengths.
