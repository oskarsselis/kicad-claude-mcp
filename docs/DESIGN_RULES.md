# KiCad design rules

General requirements for schematics and PCBs built with this MCP server.
They apply to every project unless the project asks otherwise. Choices for a
single board (footprints, fab, ERC exceptions) belong in that project's own
notes.

The schematic rules are also sent to every MCP client in the server's
instructions (`server.py`).

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

### Symbols

- Resistors: `Device:R_Small`. Capacitors: `Device:C_Small`.
- Capacitors: Value holds only the capacitance. The voltage rating goes in a
  `Voltage` field shown on the line below. The dielectric goes in a hidden
  `Dielectric` field.
- ICs show their MPN instead of the value.
- Supply rails use power symbols, including connector rails such as VBUS. A
  rail without its own library symbol uses a similar one with the net as its
  value.
- GND symbols face down with the "GND" text hidden. Supply symbols face up.
- Power-symbol references are hidden.
- Rotate or mirror symbols where that gives a cleaner layout, e.g. a
  back-to-back MOSFET pair.

### Text

- Reference and value sit on two separate lines. They must not overlap each
  other, the symbol or wires.
- Text size follows the project's default text size (Schematic Setup).
- KiCad treats a symbol as one rectangle around its body, pins and text.
  Keep other symbols out of that rectangle, or they print as overlapping.

### Wiring and labels

- Wire each supporting part (resistor, capacitor, LED, diode, transistor)
  to the IC pin it serves, and put the net label on that wire.
- Functional blocks connect to each other by labels.
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
