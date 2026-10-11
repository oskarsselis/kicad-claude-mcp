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

## 3. Place order

Place the parts in this order; each group goes where the ones before it
leave room.

### 3.1 Connectors, mounting holes, mechanical parts

- These are fixed in step 1.4: connectors on the board edges, opening
  outwards; mounting holes (M3 by default) where the project wants them.

### 3.2 Protection parts

- TVS diodes, fuses and ESD parts sit right at the connector they protect,
  between the connector and the rest of the circuit, with their pads facing
  the connector pins.

### 3.3 Main power path

- Keep the main power path in a straight line from input to output where
  possible: connector → protection → sense resistor → switches → output
  connector. Orient parts so their pads face along the path.
- No part of another net may sit on the power path.

### 3.4 Main ICs

- Place each main IC near the connector or signal source it depends on most.
- Rotate ICs so high-speed or differential pins face their connector, and
  keep the lane between them clear.

### 3.5 Power converters

- A converter, its inductor and its capacitors form one tight group, placed
  and moved as a unit:
  - input capacitor right at VIN, its ground pad next to the converter's
    ground (exposed pad);
  - inductor right at SW;
  - output capacitor at the inductor's output, its ground pad facing the
    converter's ground;
  - bootstrap, soft-start, frequency and feedback parts at their pins.
- Keep the switching loops (input capacitor - IC - ground, SW - inductor -
  output capacitor - ground) as small as possible.

### 3.6 Supporting parts

- Pull-ups, pull-downs, dividers, level shifters and decoupling capacitors
  sit next to the pin or part they serve; decoupling capacitors as close as
  possible, with the connecting pad facing the pin.
- Level shifters and other signal circuits sit near the connector they
  serve, out of the power path.

### 3.7 Temperature sensors

- Place temperature sensors near the heat source they monitor, away from
  other heat sources.

### 3.8 Other

- Test points, LEDs and the remaining parts: next to the net or part they
  belong to.
- Group related parts, and make the board as compact as these rules allow.
  Report what else would shrink it (for example moving a connector).

## 4. Placement rules

Check these on the placed board; they should not grow clearances or the
board by much.

### 4.1 Decoupling

- Decoupling capacitors sit at the pin they serve, the connecting pad facing
  the pin.
- Where one pin has several capacitors, the smallest value sits closest.
- Each capacitor gets a short via to GND right at its ground pad: leave room
  for it when placing (a 0.6 mm via touching the pad edge).

### 4.2 Switching loops

- Keep the switching loops as small as possible: input capacitor - switch -
  GND, and switch node - inductor - output capacitor - GND. Measure and
  report their areas (pad-centre polygons).

### 4.3 High voltage and low voltage

- Separate high-voltage nets from low-voltage logic by the IPC-2221B
  spacing for the highest working voltage between them (peak DC or AC):

  | Voltage | Outer conductor, bare (B2) | Component lead or pad, uncoated (A6) |
  |---|---|---|
  | 0–15 V | 0.1 mm | 0.13 mm |
  | 16–30 V | 0.1 mm | 0.25 mm |
  | 31–50 V | 0.6 mm | 0.4 mm |

- Find the highest voltage from the datasheets (supply, clamp, gate drive),
  list the high-voltage nets, and add a DRC clearance rule between them and
  every other net for tracks, vias and pours (`add_drc_rule`). Pads within
  one footprint keep the manufacturer's spacing; check that pads of
  different parts are at least the A6 value apart.
- Test a new rule once on a copy of the board (e.g. a track 0.05 mm too
  close) so it is known to fire.

## 5. Route, in this order

Run DRC after each stage and look at every copper layer before moving on.

Track geometry, for every stage:

- Tracks change direction in 45-degree steps; avoid 90-degree corners (use
  a 45-degree chamfer instead). Other angles only for RF/antenna lines.
- Make the 45-degree cut long enough to read as one, not as a clipped 90:
  aim for 0.5–0.8 mm per leg (several track widths), shorter only where pads
  or other copper leave no room. Autorouters on a fine grid tend to cut
  corners by a single grid step; widen those.
- Long runs between groups go horizontal and vertical, with 45-degree
  corners; 45-degree segments are for short jogs and corners.
- Tracks end at pad centres.
- Vias never touch pads: keep a solder-mask dam between via and pad (via
  ring at least 0.15 mm from the pad edge; the fab minimum mask web is
  0.1 mm), joined by a short stub from the pad centre. Via-in-pad only for
  thermal vias in exposed or power pads, tented or plugged.
- Parts may move a little to make room for clean routing, and two parts
  may swap places when that removes a crossing (nets unchanged; re-check DRC
  and the placement rules after moving them).
- A signal track must not cut a high-current pour in two (e.g. a gate track
  across a MOSFET pair's common-source pour): route it around the pour.

### 5.1 High-current power paths

- Route high-current paths with pours (or wide traces where a pour doesn't
  fit), solidly connected to their pads. Copy the pour on the other outer
  layer and stitch both with vias to double the copper and spread heat.
- Sense connections (e.g. to a current-sense resistor) are thin Kelvin
  traces from the resistor's own pads, not taps on the pour.
- Draw pour outlines along the path only, so they don't block other routing;
  the fill keeps clearance from other nets by itself.

### 5.2 GND

- The GND layers are solid planes over the whole board.
- Every GND pin connects directly to the GND layer through its own via, on a
  short stub from the pad centre with a solder-mask dam between pad and via
  (no via touching the pad). No GND tracks between parts. Where no via fits,
  a connector's GND contact may join the connector's own GND/shield pad.
- Place the GND vias before the remaining routing, so later traces go
  around them. A GND via may sit inside another net's pour; the pour keeps
  clear of it.

### 5.3 High-speed and differential pairs

- Matched, short, no vias where they can be avoided, no stubs; keep the
  pair's width and gap from its net class all the way, with symmetric 45°
  bends (the inner line turns earlier so the gap stays constant).
- At a connector with a fixed pin pitch, neck the pair down right at the
  pads; exempt the breakout inside the connector's courtyard from the gap
  rule.
- USB-C has two contacts per line: join them on the same layer without
  vias where possible (e.g. one line loops around the inner end of the
  contact between, the other joins past the free end of the next one), with
  the shortest stubs. If vias are unavoidable, give both lines the same
  topology, since KiCad counts via barrels in the net length.

### 5.4 Switching converter nets

- SW short and wide straight to the inductor, bootstrap capacitor right at
  its pins, input capacitor on VIN/GND, output capacitor at the inductor.
- Feedback, soft-start, frequency and bias nets short and away from SW.

### 5.5 Remaining signals

- Route everything else (by hand or with the autorouter around the copper
  already placed), at the net class widths.
- Short local connections with one natural path (sense, enable and gate
  lines, strap and LED resistors) first, longer nets around them. Pins
  walled in by other nets on both outer layers get their vias planned by
  hand (smaller vias, e.g. 0.5/0.2 mm, where 0.6/0.3 doesn't fit).

### 5.6 Thermal vias

- Put thermal vias under exposed pads unless the footprint has them (e.g.
  0.3 mm drill / 0.6 mm pad at about 0.9 mm pitch, connected solidly), and
  under hot parts' power pads (MOSFET drains) to copper on the other side.
- Via-in-pad: ask the fab to tent or plug them, so solder doesn't wick away.

## Checks

- DRC with schematic parity: 0 errors. Explain every remaining warning.
- Render the board and look at it.
- Report key distances: decoupling pad to pin, power path lengths and
  differential pair lengths.
