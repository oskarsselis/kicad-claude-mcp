"""KiCAD-Claude MCP server entry point.

Phases enabled here so far:
    0 — ping (health check)
    1 — project management (create_project, set_project, get_project_state,
        list_components)
    2 — library indexing (index_libraries, list_libraries, search_symbol,
        search_footprint, get_symbol_details)
    3 — schematic editing (add_symbol, remove_symbol, move_symbol, add_wire,
        add_label, add_power_symbol, add_no_connect, list_pins, get_pin_position)
    4 — sourcing (check_availability, find_or_fetch_symbol, import_vendor_zip,
        list_vendor_parts)
    5 — PCB editing (set_board_outline, list_footprints, add_footprint,
        move_footprint, place_footprints_grid, add_track, add_via)
    6 — autorouting (autoroute_pcb, export_dsn, import_ses) via Freerouting
    7 — validation (run_erc, run_drc) via kicad-cli + JSON parsing
    8 — hierarchical sheets + multi-layer PCBs (extra-spec)
    9 — manufacturing outputs (gerbers, drill, pos, BOM, netlist, render, SVG,
        export_fab_package)
   10 — design rules + net classes + fab presets + auto-annotation +
        update_pcb_from_schematic (closes the schematic↔PCB loop)
   11 — copper zones + mounting holes + silk + fiducials + sourcing-enriched BOM
   12 — diff pairs + length tuning (meander) + schematic buses
   13 — STEP 3D + custom DRC rules + multi-board + symbol/footprint editor
   14 — signal integrity (impedance) + thermal (IPC-2152) + RF (CPW, stitching) +
        EMC heuristics
   15 — panelization (rows × cols + mouse bites) + SPICE (ngspice wrapper) +
        thermal network + crosstalk + return-path continuity (closed-form FEM)

Future phases register additional tool groups via the same `register(mcp)`
pattern.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env early so adapters can read API credentials at import/use time.
load_dotenv(Path(__file__).parent / ".env")

from kicad_claude.tools.emc import register as register_emc_tools  # noqa: E402
from kicad_claude.tools.library import register as register_library_tools  # noqa: E402
from kicad_claude.tools.library_create import register as register_library_create_tools  # noqa: E402
from kicad_claude.tools.manufacturing import register as register_manufacturing_tools  # noqa: E402
from kicad_claude.tools.panelization import register as register_panelization_tools  # noqa: E402
from kicad_claude.tools.pcb import register as register_pcb_tools  # noqa: E402
from kicad_claude.tools.project import register as register_project_tools  # noqa: E402
from kicad_claude.tools.rf import register as register_rf_tools  # noqa: E402
from kicad_claude.tools.routing import register as register_routing_tools  # noqa: E402
from kicad_claude.tools.rules import register as register_rules_tools  # noqa: E402
from kicad_claude.tools.schematic import register as register_schematic_tools  # noqa: E402
from kicad_claude.tools.signal_integrity import register as register_signal_integrity_tools  # noqa: E402
from kicad_claude.tools.simulation import register as register_simulation_tools  # noqa: E402
from kicad_claude.tools.sourcing import register as register_sourcing_tools  # noqa: E402
from kicad_claude.tools.spice import register as register_spice_tools  # noqa: E402
from kicad_claude.tools.sync import register as register_sync_tools  # noqa: E402
from kicad_claude.tools.thermal import register as register_thermal_tools  # noqa: E402
from kicad_claude.tools.validation import register as register_validation_tools  # noqa: E402
from kicad_claude.utils.logging import setup_logging  # noqa: E402

logger = setup_logging()

# Sent to every MCP client on connect, so these conventions apply on any
# machine without local configuration.
INSTRUCTIONS = """\
KiCad MCP: conventions for schematics and PCBs (full lists:
docs/SCHEMATIC_RULES.md and docs/PCB_RULES.md in the server's repository).
They apply to every project unless the project asks otherwise.

General: don't add, remove or change any part, value or net from the source
data (CSV, netlist, datasheet); if something can't be mapped, stop and ask.
Check that each footprint's pad numbers match its symbol's pin numbers.

Coordinates: millimetres, origin at the bottom-left of the page, Y pointing up.
On A4 the drawing frame spans x 10..287, y 8.28..198.28 (A3: x 10..410,
y 7.64..284.64). The title block fills the bottom-right corner (about 110 x 34
mm): keep it clear. Put the circuit in the upper/middle part of the page.

Projects: one folder per project, e.g. create_project(path="<parent>/<name>",
name="<name>").

Functional blocks:
- Put each functional block (e.g. "USB PD controller", "Level shifter",
  "3V3 DC/DC") in a dashed rectangle with its title in the top-left corner
  (add_block). Blocks must not overlap; keep each block's symbols, wires and
  text inside it, below the title. Place related blocks next to each other
  (e.g. a regulator directly below the circuit it feeds).
- Tile the blocks with one equal gap (e.g. 5.08 mm) between neighbouring
  blocks and between blocks and the drawing frame or title block
  (get_sheet_layout gives both); centre each circuit in its block, keeping
  its connection points on the grid.
- Connect blocks with net labels and power symbols, never with wires across
  a block border.

Grid and placement (the tools refuse off-grid and off-frame input and suggest
a valid position; plan for it instead of trial and error):
- Every connection point (pin end, wire end, label, junction) is on the
  100 mil (2.54 mm) grid. Check pin offsets with get_symbol_details or
  list_pins. Symbols whose pins sit 150 mil from their origin (Device:R,
  Device:C, LED, ...) need the origin half a step (1.27 mm) off grid along
  the pin axis; R_Small/C_Small pins are 100 mil from the origin.
- Wire exactly to the positions list_pins reports. Junctions are added
  automatically where three or more connections meet, also when a symbol's
  pin lands on an existing wire.

Symbols:
- Resistors: Device:R_Small. Capacitors: Device:C_Small.
- Value field: the short value only ("10µF", "5.1kΩ", "5mΩ"), never a
  description such as "TVS, 22 V standoff" or "N-MOSFET, 50 V". Keep the full
  text in the hidden Description (add_symbol(description=...)).
- ICs, MOSFETs, TVS diodes and LEDs: the part number is the value (and in the
  MPN field).
- Secondary information worth seeing (a capacitor's voltage, a resistor's
  tolerance or power, an inductor's saturation current) goes in its own field
  shown below the value (show_fields): it is drawn smaller and grey. Other
  details (dielectric, ...) stay in hidden fields.
- Capacitors: Value = capacitance, "Voltage" field shown, "Dielectric" hidden.
- Power symbols for supply rails (GND, +3V3, VBUS, ...): GND symbols face
  down with the "GND" text hidden (hide_value=True); supply symbols face up.
  A rail without its own library symbol uses a similar one with the net as
  value (add_power_symbol(net="+5V_PD", symbol="+5V")).
- Use mirror="y" where rotation alone can't give the needed pin order (e.g. a
  back-to-back MOSFET pair with both gates down).
- Pins of type no_connect that the library hides need no no-connect flag;
  flag only visible unused pins.
- Add a PWR_FLAG to every supply and ground net that no power-output pin
  drives (e.g. power arriving through a connector), or ERC reports
  power_pin_not_driven.

Wiring and labels:
- Connect each supporting part (resistors, capacitors, LEDs, diodes,
  transistors) to the IC pin it serves with a wire; put the net label on that
  wire. Connections between functional blocks use labels alone.
- Decoupling capacitors sit vertically right at the pin they serve, each with
  its own ground symbol. Pull-ups, pull-downs and gate resistors sit next to
  the part they belong to.
- No wire run longer than a quarter of the sheet width (A4: 74 mm, A3: 105
  mm): for longer connections put a net label at both ends instead.
- Net names: when the user gives a source for them (a connection CSV, a
  netlist, a reference design, ...) or says which to use, use those names
  exactly. Only when no source is given, name nets by function, prefixed by
  their block, e.g. PD_SCL, PD_SDA, PD_OTP, DCDC_FB.
- Power wires are red: after wiring, call color_power_wires (colours every
  wire connected to a power symbol; ground stays default).
- Test points sit on the wire of the net they probe, in the circuit next to
  the part it concerns (e.g. VBUS at the sense resistor); no lone test points.
- A label's text must lie along its own wire: make the wire at least as long
  as the text and, at a stub's free end, point the label back towards the
  pin. Text sticking out past a wire end looks like it hangs off the tip.
- Wires may cross where that keeps the layout simple; a crossing must never
  join two nets (only wire ends create connections).
- Keep it compact: short wires, few bends, parts close to the pins they serve.

Text and spacing:
- Reference/Value text uses the project's default text size (KiCad's
  Schematic Setup, 50 mil unless changed); set_schematic_text_size changes it
  for symbols placed afterwards.
- Reference and value are placed automatically beside each symbol, on the
  side without pins, Reference above Value. Keep this default so text sits
  in the same place relative to every symbol; avoid text_side.
- Text must not overlap wires, symbols or other text.
- KiCad treats a symbol as one rectangle around its body, pins and visible
  text, so the empty area under a long value still belongs to it: keep other
  symbols out of that rectangle, or they are plotted as overlapping.
- Capacitors and resistors hanging off a horizontal wire need a short stub
  (one grid step) so their text clears the wire.

Finishing: after changing a schematic, call verify_schematic. Only report the
schematic as done when `ok` is true; otherwise fix every item in `problems`
(overlapping text, text on wires, wires through symbols, labels off their
wire, crowding, grid and frame, blocks, long wires, uncoloured power wires,
lone test points, long values, Reference/Value layout). Compare `nets` with the intended circuit,
and open the PDF it returns and look at it: the checks cannot judge
readability or whether the circuit does what was asked. Report ERC results
exactly as returned.

PCB layout, in this order (when a step is already done, check it and move
on):
1. Prepare
1.1 Run ERC and fix all errors. Check every footprint against its datasheet
    when one is available (pads, pitch, exposed pad, span, pin 1, drills,
    body); if you can't read it, ask the user to check that footprint.
    Propose fixes; don't change an agreed footprint without asking.
1.2 Sync the schematic to the PCB (add_footprint for each part, then
    update_pcb_from_schematic): footprints carry the schematic's fields, BOM
    flags and symbol links; re-sync after every schematic change. Hide all
    references and values (set_footprint_text_visibility).
1.3 Stackup: prefer 4 layers, signal/power, GND, GND, signal/power
    (set_layer_count(4); set_copper_layer F.Cu/B.Cu "mixed", In1.Cu/In2.Cu
    "power" named as GND planes, no pour until asked). set_layer_count(4)
    sets the default stackup: 1.6 mm, 1 oz copper, prepreg 7628 RC46%
    0.175 mm (Dk 4.74), core 1.03 mm (Dk 4.6), prepreg 0.175 mm. Disable F.Fab and
    B.Fab (set_layers_enabled, after set_layer_count). Design rules from the
    chosen fab's published capabilities (apply_fab_preset); state which
    limits the fab doesn't publish.
1.4 Mechanical constraints first: board outline, mounting holes (if used;
    ask when the source doesn't say; M3 by default) and connector positions,
    before placing anything else. The outline has rounded corners (default
    1 mm radius, set_board_outline(corner_radius_mm=1.0)); keep parts clear
    of them. Connectors on the board edges, opening outwards.
2. Set rules before placing (don't route or pour yet)
2.1 Group nets into classes: high current, low power, signal (Default), high
    speed or differential (add_net_class, add_diff_pair_class,
    assign_net_class with the exact board net names; label nets carry a "/"
    prefix).
2.2 Clearance and constraints at the fab's minimums (apply_fab_preset /
    set_design_rules); net class clearance = fab minimum; vias 0.6/0.3 mm.
2.3 Track widths from the current with IPC-2152
    (solve_trace_width_for_current; 10 C rise). Signal 0.1-0.3 mm (default
    0.2), power 0.3-0.7 mm; pours for high current (more than a 0.7 mm track
    carries). State the design currents; ask when unknown.
2.4 Impedance and length rules for controlled-impedance nets and
    differential pairs: width/gap from the stackup
    (calculate_differential_impedance), DRC rules for width, gap, uncoupled
    length, skew and length (add_drc_rule). USB 2.0: 90 ohm, skew <= 1.27 mm.

3. Place order (each group goes where the ones before leave room)
3.1 Connectors, mounting holes, mechanical parts (fixed in 1.4).
3.2 Protection parts (TVS, fuses, ESD) right at the connector they protect,
    pads facing its pins.
3.3 Main power path in a straight line from input to output where possible
    (connector, protection, sense resistor, switches, output connector),
    parts oriented along it, no part of another net on it.
3.4 Main ICs near the connector or signal source they depend on most;
    high-speed/differential pins facing their connector, lane kept clear.
3.5 Power converters with their inductor and capacitors as one tight group,
    placed as a unit: input cap at VIN with its ground next to the IC ground,
    inductor at SW, output cap at the inductor with its ground facing the IC
    ground; small switching loops.
3.6 Supporting parts (pull-ups, dividers, level shifters, decoupling) next
    to the pin or part they serve, connecting pad facing the pin; signal
    circuits near their connector, out of the power path.
3.7 Temperature sensors near the heat source, away from other heat sources.
3.8 Other parts (test points, LEDs, ...) next to what they belong to. Keep
    the board as compact as these rules allow; report what else would
    shrink it.

4. Placement rules (check on the placed board; don't grow the board much)
4.1 Decoupling caps at the pin they serve, smallest value closest, room for
    a short GND via right at each ground pad.
4.2 Switching loops (input cap - switch - GND; SW - inductor - output cap -
    GND) as small as possible; report their areas.
4.3 Separate high-voltage nets from low-voltage logic by IPC-2221B spacing
    for the highest working voltage (16-30 V: 0.1 mm bare conductor, 0.25 mm
    pad/lead; 31-50 V: 0.6 / 0.4 mm): DRC clearance rule for tracks, vias
    and pours between the HV nets and all others; pads of different parts at
    least the pad value apart. Test a new rule once on a board copy.

PCB checks: run_drc with 0 errors and every warning explained; render the
board and look at it; report decoupling pad-to-pin distances, power path
and differential pair lengths.
"""

mcp = FastMCP("kicad-claude", instructions=INSTRUCTIONS)


@mcp.tool()
def ping() -> str:
    """Health check. Returns 'pong' if the server is reachable."""
    logger.info("ping called")
    return "pong"


register_project_tools(mcp)
register_library_tools(mcp)
register_schematic_tools(mcp)
register_sourcing_tools(mcp)
register_pcb_tools(mcp)
register_routing_tools(mcp)
register_validation_tools(mcp)
register_manufacturing_tools(mcp)
register_rules_tools(mcp)
register_sync_tools(mcp)
register_library_create_tools(mcp)
register_signal_integrity_tools(mcp)
register_thermal_tools(mcp)
register_rf_tools(mcp)
register_emc_tools(mcp)
register_panelization_tools(mcp)
register_spice_tools(mcp)
register_simulation_tools(mcp)


if __name__ == "__main__":
    logger.info("starting kicad-claude MCP server (stdio)")
    mcp.run()
