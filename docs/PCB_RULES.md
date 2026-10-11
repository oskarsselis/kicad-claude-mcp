# KiCad PCB layout rules

General requirements for PCB layouts made with this MCP server, in the order
of the work. They apply to every project unless the project asks otherwise.
Choices for a single board (fab, stackup details, footprints) belong in that
project's own notes. Schematic rules are in
[SCHEMATIC_RULES.md](SCHEMATIC_RULES.md).

These rules are also sent to every MCP client in the server's instructions
(`server.py`). When a step is already done (e.g. an earlier session set the
stackup), check it and move on.

## General

- Don't add, remove or change any part, value or net from the source data
  (CSV, netlist, datasheet). If something can't be mapped, stop and ask.
- Check that each footprint's pad numbers match its symbol's pin numbers.
- Commit and push only when asked.

## 1. Prepare

### 1.1 ERC and footprints

- Run ERC and fix all errors. Explain every remaining warning.
- Check every footprint against its datasheet when one is available: pad
  size and pitch, exposed pad, overall span, pin 1 and numbering, drill and
  pad sizes for through-hole parts, body outline. If a datasheet can't be
  read (none given, download blocked, image-only drawing), ask the user to
  check that footprint.
- Report differences and propose fixes; don't change an agreed footprint
  without asking.

### 1.2 Sync the schematic to the PCB

- Import from the schematic (`add_footprint` for each part, then
  `update_pcb_from_schematic`). Footprints carry the schematic's fields, BOM
  flags and symbol links, so KiCad's Update PCB from Schematic matches them.
- Re-sync after every schematic change; DRC with schematic parity must show
  no differences.
- Hide all references and values.

### 1.3 Stackup

- Prefer a 4-layer board: signal/power, GND, GND, signal/power.
  - F.Cu and B.Cu: type mixed (signals and power).
  - In1.Cu and In2.Cu: GND planes (type power). No pour until asked.
- Default 4-layer stackup (PCBWay standard), set by `set_layer_count(4)`:

  | Layer | Material | Thickness | Dk |
  |---|---|---|---|
  | F.Cu | copper, 1 oz finished | 0.035 mm | |
  | Prepreg | 7628 RC46% | 0.175 mm | 4.74 |
  | In1.Cu | copper, 1 oz finished | 0.035 mm | |
  | Core | FR4 | 1.03 mm | 4.6 |
  | In2.Cu | copper, 1 oz finished | 0.035 mm | |
  | Prepreg | 7628 RC46% | 0.175 mm | 4.74 |
  | B.Cu | copper, 1 oz finished | 0.035 mm | |

  Board thickness 1.6 mm nominal. Use another stackup only when the project
  or fab asks for it.
- F.Fab and B.Fab are disabled.
- Set the design rules from the chosen fab's published capabilities. State
  which limits the fab doesn't publish, which values may cost extra, and
  check the fab's standard stackup (thickness, copper weights).

### 1.4 Mechanical constraints first

- Set the board outline, mounting holes (if used) and connector positions
  before placing anything else.
- The board outline has rounded corners (default radius 1 mm,
  `set_board_outline(corner_radius_mm=...)`); keep parts clear of them.
- Connectors sit on the board edges, opening outwards.
- Ask whether mounting holes are wanted when the source data doesn't say.
  Mounting holes are M3 (3.2 mm hole) unless the project says otherwise.

## 2. Set rules before placing

Set every rule before placing or routing. Don't route or pour in this step.

### 2.1 Net classes

- Group the nets into classes: high current, low power, signal (KiCad's
  `Default` class) and high speed or differential.
- Assign nets by their exact board names: nets named by labels carry a `/`
  prefix (`/USB_D_P`), nets named by power symbols don't (`VBUS`).

### 2.2 Clearance and constraints

- Set the board constraints (clearance, track width, via, annular ring,
  hole clearance, hole to hole, copper to edge, solder-mask web, text) to
  the fab's minimums, and use the fab's minimum clearance in the net classes.
- Vias: 0.6 mm pad / 0.3 mm drill by default (annular ring 0.15 mm).

### 2.3 Track widths

- Size track widths from the current with IPC-2152
  (`solve_trace_width_for_current`, `calculate_trace_current_capacity`;
  10 °C rise unless the project says otherwise). State the design currents
  used; ask when they aren't known.
- Default signal track width: 0.1–0.3 mm (0.2 mm by default).
- Default power track width: 0.3–0.7 mm.
- Use pours for high current: anything more than a 0.7 mm track carries
  (about 1.7 A at 10 °C rise on 1 oz outer copper).

### 2.4 Impedance and length

- Define impedance and length rules for every controlled-impedance net and
  differential pair: compute width and gap for the stackup
  (`calculate_differential_impedance`, `solve_trace_width_for_impedance`),
  put them in the pair's net class, and add DRC rules for width, gap,
  uncoupled length, skew and maximum length (`add_drc_rule`).
- USB 2.0: 90 Ω differential; skew ≤ 1.27 mm (50 mil).
- The impedance formulas are approximations: confirm critical impedances
  with the fab's calculator before ordering.

## Placement

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

## Checks

- DRC with schematic parity: 0 errors. Explain every remaining warning.
- Render the board and look at it.
- Report key distances: decoupling pad to pin, power path lengths and
  differential pair lengths.
