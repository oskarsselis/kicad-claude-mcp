"""Schematic verification: everything worth checking before calling a schematic done.

`verify(project_dir, project_name)` runs, for the root sheet and every
sub-sheet:

- KiCAD's ERC (through kicad-cli, full report)
- a readable netlist (net -> REF.pin) and nets with a single connection
- geometry: connection points off the 100 mil grid, items outside the
  drawing frame or on the title block
- crowding: KiCAD plots symbols whose bounding boxes (fields included)
  overlap a second time, so a reference drawn twice in the SVG export means
  that symbol collides with another one
- drawing style: functional blocks (dashed rectangles) that overlap or are
  crossed by wires, symbols or text; wires longer than a quarter of the sheet
  width; power wires not coloured; test points not wired into the circuit;
  long values; Reference/Value not stacked the usual way
- a PDF of the schematic, for looking at it

The parsing helpers are pure functions so they can be tested without KiCAD.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import sexpdata

from kicad_claude.adapters import kicad_cli, sch_editor as ed, sch_io
from kicad_claude.adapters.sch_io import find_child, find_children, head_of, is_call

TITLE_BLOCK_W_MM = ed.TITLE_BLOCK_W_MM
TITLE_BLOCK_H_MM = ed.TITLE_BLOCK_H_MM
BLOCK_GAP_TOLERANCE_MM = 0.1


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #


def sheet_files(root_sch: Path) -> list[Path]:
    """Root schematic plus every sub-sheet file it (transitively) references."""
    root_sch = Path(root_sch)
    out: list[Path] = []
    todo = [root_sch]
    while todo:
        path = todo.pop(0)
        if path in out or not path.is_file():
            continue
        out.append(path)
        tree = sch_io.parse_file(path)
        todo += [path.parent / s["filename"] for s in ed.list_sheets(tree) if s["filename"]]
    return out


# --------------------------------------------------------------------------- #
# Netlist
# --------------------------------------------------------------------------- #


def parse_netlist(text: str) -> dict[str, list[str]]:
    """Map net name -> ["REF.pin", ...] from a kicadsexpr netlist."""
    data = sexpdata.loads(text)
    nets_node = find_child(data, "nets") or []
    nets: dict[str, list[str]] = {}
    for net in find_children(nets_node, "net"):
        name_node = find_child(net, "name")
        name = name_node[1] if name_node else "?"
        nodes = []
        for node in find_children(net, "node"):
            ref, pin = find_child(node, "ref"), find_child(node, "pin")
            if ref and pin:
                nodes.append(f"{ref[1]}.{pin[1]}")
        nets[name] = nodes
    return nets


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _connection_points(tree: list) -> list[tuple[str, float, float]]:
    """(description, x, y) for every connection point in a sheet, KiCAD coords."""
    pts: list[tuple[str, float, float]] = []
    for node in tree[1:]:
        h = head_of(node)
        if h in ("wire", "bus"):
            for xy in find_children(find_child(node, "pts") or [], "xy"):
                pts.append((f"{h} end", float(xy[1]), float(xy[2])))
        elif h in ("label", "global_label", "hierarchical_label"):
            at = find_child(node, "at")
            pts.append((f"{h} {node[1]}", float(at[1]), float(at[2])))
        elif h in ("junction", "no_connect", "bus_entry"):
            at = find_child(node, "at")
            pts.append((h.replace("_", "-"), float(at[1]), float(at[2])))
        elif h == "sheet":
            for pin in find_children(node, "pin"):
                at = find_child(pin, "at")
                pts.append((f"sheet pin {pin[1]}", float(at[1]), float(at[2])))
    page_h = ed.page_height_mm(tree)
    for s_node in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s_node, "Reference") or "?"
        try:
            pins = ed.list_pins_for_symbol(tree, ref)
        except (KeyError, ValueError):
            continue
        for p in pins:
            x, y = p["position_mm"]
            pts.append((f"{ref} pin {p['number']}", x, page_h - y))
    return pts


def geometry_issues(tree: list, sheet: str) -> dict[str, list[dict]]:
    """Off-grid connection points, and items outside the frame or on the title block."""
    page_h = ed.page_height_mm(tree)
    w, h = ed.page_size_mm(tree)
    m = ed.SHEET_FRAME_MARGIN_MM
    tb = (w - m - TITLE_BLOCK_W_MM, h - m - TITLE_BLOCK_H_MM)

    def entry(what: str, x: float, y: float) -> dict:
        return {"sheet": sheet, "item": what, "position_mm": [ed.round_mm(x), ed.round_mm(page_h - y)]}

    off_grid, outside, on_title = [], [], []
    points = _connection_points(tree)
    for what, x, y in points:
        if not (ed._on_grid(x) and ed._on_grid(y)):
            off_grid.append(entry(what, x, y))

    # Symbols by their outline, everything else by its points.
    boxes: list[tuple[str, float, float, float, float]] = []
    for s_node in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s_node, "Reference") or "?"
        lib_id = find_child(s_node, "lib_id")
        sym_def = ed.find_lib_symbol_def(tree, lib_id[1]) if lib_id else None
        at = find_child(s_node, "at")
        if sym_def is None or at is None:
            continue
        x0, y0, x1, y1 = ed.symbol_outline(
            sym_def, float(at[1]), float(at[2]), int(float(at[3])), ed.instance_mirror(s_node)
        )[0]
        boxes.append((ref, x0, y0, x1, y1))
    boxes += [(what, x, y, x, y) for what, x, y in points if " pin " not in what]

    for what, x0, y0, x1, y1 in boxes:
        if x0 < m or y0 < m or x1 > w - m or y1 > h - m:
            outside.append(entry(what, (x0 + x1) / 2, (y0 + y1) / 2))
        elif x1 > tb[0] and y1 > tb[1]:
            on_title.append(entry(what, (x0 + x1) / 2, (y0 + y1) / 2))
    return {"off_grid": off_grid, "outside_frame": outside, "on_title_block": on_title}


# --------------------------------------------------------------------------- #
# Text placement (from the SVG export: exact positions of everything drawn)
# --------------------------------------------------------------------------- #

_SVG_TEXT = re.compile(
    r'(?:<g transform="rotate\((?P<a>[-\d.]+) (?P<cx>[-\d.]+) (?P<cy>[-\d.]+)\)">\s*)?'
    r'<text x="(?P<x>[-\d.]+)" y="(?P<y>[-\d.]+)"\s*textLength="(?P<len>[-\d.]+)" '
    r'font-size="(?P<fs>[-\d.]+)"[^>]*?text-anchor="(?P<anchor>\w+)"[^>]*>(?P<text>[^<]*)</text>'
)


def svg_text_boxes(svg: str) -> list[tuple[str, float, float, float, float]]:
    """(text, x0, y0, x1, y1) in page mm (Y down) for every text in a KiCAD SVG.

    KiCAD writes each text as an invisible <text> (for search) at its anchor,
    with its exact length; rotated text sits in a rotate() group. Identical
    entries from KiCAD's over-plotting are merged.
    """
    boxes = set()
    for m in _SVG_TEXT.finditer(svg):
        x, y, length = float(m["x"]), float(m["y"]), float(m["len"])
        h = float(m["fs"]) * 0.75  # font-size is in pt-scaled units; glyph height in mm
        start = {"start": 0.0, "middle": -length / 2, "end": -length}[m["anchor"]]
        corners = [(start, -h), (start + length, -h), (start, h * 0.2), (start + length, h * 0.2)]
        if m["a"]:
            a = math.radians(float(m["a"]))
            corners = [(dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a))
                       for dx, dy in corners]
        xs = [x + dx for dx, _ in corners]
        ys = [y + dy for _, dy in corners]
        boxes.add((m["text"], round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)))
    return sorted(boxes)


def text_issues(svg: str, page_w: float, page_h: float, flip_h: float) -> dict[str, list[dict]]:
    """Texts that cross the drawing frame, and pairs of texts that overlap.

    The frame's border labels (wholly outside the frame) and the title block
    are part of the drawing sheet and ignored.
    """
    m = ed.SHEET_FRAME_MARGIN_MM
    tb_x, tb_y = page_w - m - TITLE_BLOCK_W_MM, page_h - m - TITLE_BLOCK_H_MM
    inside = []
    crossing = []
    for t, x0, y0, x1, y1 in svg_text_boxes(svg):
        if x1 < m or y1 < m or x0 > page_w - m or y0 > page_h - m:
            continue  # border label
        if x0 >= tb_x and y0 >= tb_y:
            continue  # title block
        if x0 < m or y0 < m or x1 > page_w - m or y1 > page_h - m:
            crossing.append({"text": t, "position_mm": [round((x0 + x1) / 2, 2), round(flip_h - (y0 + y1) / 2, 2)]})
        inside.append((t, x0, y0, x1, y1))

    overlaps = []
    eps = 0.05
    for i, (ta, ax0, ay0, ax1, ay1) in enumerate(inside):
        for tb, bx0, by0, bx1, by1 in inside[i + 1:]:
            if ax0 + eps < bx1 and bx0 + eps < ax1 and ay0 + eps < by1 and by0 + eps < ay1:
                overlaps.append({
                    "texts": [ta, tb],
                    "position_mm": [round((max(ax0, bx0) + min(ax1, bx1)) / 2, 2),
                                    round(flip_h - (max(ay0, by0) + min(ay1, by1)) / 2, 2)],
                })
    return {"text_outside_frame": crossing, "text_overlaps": overlaps}


def _segment_hits_box(a, b, box) -> bool:
    """True if segment a-b passes through the open rectangle `box` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    if x0 >= x1 or y0 >= y1:
        return False
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q <= 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 >= t1:
                return False
    return True


def wire_segments(tree: list) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for node in tree[1:]:
        if head_of(node) in ("wire", "bus"):
            pts = [(float(xy[1]), float(xy[2])) for xy in find_children(find_child(node, "pts") or [], "xy")]
            out += list(zip(pts, pts[1:]))
    return out


def symbol_bodies(tree: list) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(reference, body box) of each non-power symbol: its graphics without pins."""
    out = []
    for s_node in ed.iter_instance_symbols(tree):
        lib_id = find_child(s_node, "lib_id")
        sym_def = ed.find_lib_symbol_def(tree, lib_id[1]) if lib_id else None
        at = find_child(s_node, "at")
        if sym_def is None or at is None or find_child(sym_def, "power") is not None:
            continue
        graphics = [c for c in sym_def[2:] if head_of(c) != "pin"]
        units = [
            [c[0], c[1]] + [g for g in c[2:] if head_of(g) != "pin"]
            for c in graphics if head_of(c) == "symbol"
        ]
        body_only = [sym_def[0], sym_def[1]] + [c for c in graphics if head_of(c) != "symbol"] + units
        box = ed.symbol_outline(
            body_only, float(at[1]), float(at[2]), int(float(at[3])), ed.instance_mirror(s_node)
        )[0]
        out.append((ed.get_symbol_property(s_node, "Reference") or "?", box))
    return out


def _on_segment(p, a, b, tol: float = 0.05) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > tol * max(1.0, abs(b[0] - a[0]) + abs(b[1] - a[1])):
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def labels_off_wire(svg: str, tree: list, flip_h: float) -> list[dict]:
    """Local labels whose text runs past the end of the wire they sit on.

    A label's text should lie along its wire for its whole length; text
    sticking out beyond the wire end looks like it hangs off the tip.
    """
    segs = wire_segments(tree)
    boxes = svg_text_boxes(svg)
    out = []
    step = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}  # file coords, Y down
    for node in tree[1:]:
        if head_of(node) != "label":
            continue
        at = find_child(node, "at")
        ax, ay, ang = float(at[1]), float(at[2]), int(float(at[3])) % 360
        same = [b for b in boxes if b[0] == node[1]]
        if not same or ang not in step:
            continue
        # The label's own text box is the one nearest its anchor.
        t, x0, y0, x1, y1 = min(same, key=lambda b: min(abs(b[1] - ax), abs(b[3] - ax)) + min(abs(b[2] - ay), abs(b[4] - ay)))
        length = (x1 - x0) if ang in (0, 180) else (y1 - y0)
        dx, dy = step[ang]
        n = max(1, int(length / 0.5))
        pts = [(ax + dx * length * k / n, ay + dy * length * k / n) for k in range(n + 1)]
        if not all(any(_on_segment(q, a, b) for a, b in segs) for q in pts):
            out.append({"label": node[1], "position_mm": [ax, round(flip_h - ay, 2)]})
    return out


def wiring_issues(svg: str, tree: list, flip_h: float) -> dict[str, list[dict]]:
    """Text lying on a wire, wires running through a symbol body, and labels
    whose text runs past the end of their wire."""
    segs = wire_segments(tree)
    m = ed.SHEET_FRAME_MARGIN_MM
    on_wire = []
    for t, x0, y0, x1, y1 in svg_text_boxes(svg):
        box = (x0 + 0.15, y0 + 0.15, x1 - 0.15, y1 - 0.15)
        if x1 < m or y1 < m:
            continue
        if any(_segment_hits_box(a, b, box) for a, b in segs):
            on_wire.append({"text": t, "position_mm": [round((x0 + x1) / 2, 2), round(flip_h - (y0 + y1) / 2, 2)]})
    through = []
    for ref, (x0, y0, x1, y1) in symbol_bodies(tree):
        box = (x0 + 0.3, y0 + 0.3, x1 - 0.3, y1 - 0.3)
        for a, b in segs:
            if _segment_hits_box(a, b, box):
                through.append({"symbol": ref, "wire": [[a[0], round(flip_h - a[1], 2)], [b[0], round(flip_h - b[1], 2)]]})
    return {
        "text_on_wires": on_wire,
        "wires_through_symbols": through,
        "labels_off_wire": labels_off_wire(svg, tree, flip_h),
    }


# --------------------------------------------------------------------------- #
# Drawing style
# --------------------------------------------------------------------------- #

MAX_VALUE_CHARS = 20
POWER_WIRE_COLOR = ed.WIRE_COLORS["red"]


def _segments_cross(a, b, c, d) -> bool:
    """True if segments a-b and c-d intersect at a point inside both (not at an end)."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 * o2 < -1e-9 and o3 * o4 < -1e-9


def _box_edges(box):
    x0, y0, x1, y1 = box
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def _crosses_border(box, block) -> bool:
    """True if `box` lies partly inside and partly outside `block`."""
    x0, y0, x1, y1 = box
    a0, b0, a1, b1 = block
    overlaps = x0 < a1 and a0 < x1 and y0 < b1 and b0 < y1
    inside = a0 <= x0 and x1 <= a1 and b0 <= y0 and y1 <= b1
    return overlaps and not inside


def _is_test_point(s_node) -> bool:
    lib_id = find_child(s_node, "lib_id")
    ref = ed.get_symbol_property(s_node, "Reference") or ""
    return bool(lib_id and "TestPoint" in str(lib_id[1])) or bool(re.fullmatch(r"TP\d+", ref))


def style_issues(tree: list) -> dict[str, list[dict]]:
    """Drawing-style checks that need only the schematic file.

    - blocks_overlapping: functional blocks (rectangles) overlapping each other
    - block_spacing: blocks whose gaps to their neighbours, the title block
      and the frame are not all equal
    - crossing_blocks: wires or symbols crossing a block border (connect
      blocks with labels; keep each part inside its block)
    - long_wires: connected wires spanning more than a quarter of the sheet
      width (use net labels at both ends instead)
    - uncolored_power_wires: wires on a power-symbol net that aren't red
    - lone_test_points: test points whose pin isn't on a wire
    - long_values: visible values with a comma or over MAX_VALUE_CHARS
      characters (descriptions belong in hidden fields)
    - ref_value_layout: Reference and Value not stacked with the Value right
      below the Reference
    """
    page_h = ed.page_height_mm(tree)
    page_w, _ = ed.page_size_mm(tree)

    def mcp(x, y):
        return [ed.round_mm(x), ed.round_mm(page_h - y)]

    blocks = list(ed.iter_blocks(tree))
    overlapping = []
    for i, (ta, a) in enumerate(blocks):
        for tb, b in blocks[i + 1:]:
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                overlapping.append({"blocks": [ta, tb]})

    crossing = []
    segs = wire_segments(tree)
    for title, block in blocks:
        edges = _box_edges(block)
        for a, b in segs:
            if any(_segments_cross(a, b, c, d) for c, d in edges):
                crossing.append({"block": title, "item": "wire", "position_mm": mcp(*a)})
        for ref, box in symbol_bodies(tree):
            if _crosses_border(box, block):
                crossing.append({"block": title, "item": ref, "position_mm": mcp(box[0], box[1])})

    long_wires = []
    limit = page_w / 4
    for g in ed.wire_groups(tree):
        pts = [e for w in g["wires"] for e in (ed._wire_ends(w) or ())]
        if not pts:
            continue
        span = max(max(x for x, _ in pts) - min(x for x, _ in pts), max(y for _, y in pts) - min(y for _, y in pts))
        if span > limit + 1e-6:
            x0, y0 = min(x for x, _ in pts), min(y for _, y in pts)
            long_wires.append({"nets": sorted(g["nets"]), "span_mm": round(span, 2),
                               "limit_mm": round(limit, 2), "position_mm": mcp(x0, y0)})

    uncolored = []
    for g in ed.power_wire_groups(tree):
        bad = [w for w in g["wires"] if ed.wire_color(w) != POWER_WIRE_COLOR]
        if bad:
            a = ed._wire_ends(bad[0])
            uncolored.append({"nets": sorted(g["power_nets"]), "wires": len(bad),
                              "position_mm": mcp(*a[0]) if a else None})

    wire_ends = [e for e in (ed._wire_ends(c) for c in tree[1:] if is_call(c, "wire")) if e]
    lone, long_values, layout = [], [], []
    for s_node in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s_node, "Reference") or "?"
        lib_id = find_child(s_node, "lib_id")
        sym_def = ed.find_lib_symbol_def(tree, lib_id[1]) if lib_id else None
        if sym_def is not None and find_child(sym_def, "power") is not None:
            continue
        if _is_test_point(s_node):
            for p in ed.list_pins_for_symbol(tree, ref):
                pt = (p["position_mm"][0], page_h - p["position_mm"][1])
                if not any(_on_segment(pt, a, b) for a, b in wire_ends):
                    lone.append({"reference": ref, "position_mm": p["position_mm"]})
        props = {pr[1]: pr for pr in find_children(s_node, "property") if len(pr) >= 3}
        value = props.get("Value")
        if value is not None and _visible(value) and (", " in value[2] or len(value[2]) > MAX_VALUE_CHARS):
            long_values.append({"reference": ref, "value": value[2]})
        shown = [n for n in ("Reference", "Value") if n in props and _visible(props[n])]
        if len(shown) == 2:
            ra, va = find_child(props["Reference"], "at"), find_child(props["Value"], "at")
            same_column = abs(float(ra[1]) - float(va[1])) < 0.01 and float(ra[3]) == float(va[3])
            if not same_column or float(va[2]) <= float(ra[2]):
                layout.append({"reference": ref})
    return {
        "blocks_overlapping": overlapping,
        "block_spacing": block_spacing_issues(tree),
        "crossing_blocks": crossing,
        "long_wires": long_wires,
        "uncolored_power_wires": uncolored,
        "lone_test_points": lone,
        "long_values": long_values,
        "ref_value_layout": layout,
    }


def block_gaps(tree: list) -> list[dict]:
    """For each block side, the gap to whatever it faces: another block, the
    title block or the drawing frame (file coords)."""
    layout = ed.sheet_layout(tree)
    fx0, fy0, fx1, fy1 = layout["frame"]
    blocks = list(ed.iter_blocks(tree))
    out = []
    for i, (title, (x0, y0, x1, y1)) in enumerate(blocks):
        others = [b for k, (_, b) in enumerate(blocks) if k != i] + [layout["title_block"]]
        across_y = [o for o in others if o[1] < y1 and y0 < o[3]]   # beside it (left/right)
        across_x = [o for o in others if o[0] < x1 and x0 < o[2]]   # above/below it
        gaps = {
            "left": min([x0 - fx0] + [x0 - o[2] for o in across_y if o[2] <= x0 + 1e-6]),
            "right": min([fx1 - x1] + [o[0] - x1 for o in across_y if o[0] >= x1 - 1e-6]),
            "top": min([y0 - fy0] + [y0 - o[3] for o in across_x if o[3] <= y0 + 1e-6]),
            "bottom": min([fy1 - y1] + [o[1] - y1 for o in across_x if o[1] >= y1 - 1e-6]),
        }
        out.append({"block": title, **{k: round(v, 3) for k, v in gaps.items()}})
    return out


def block_spacing_issues(tree: list) -> list[dict]:
    """Blocks whose gaps (to each other, the title block and the frame) are
    not all the same."""
    gaps = block_gaps(tree)
    values = [g[s] for g in gaps for s in ("left", "right", "top", "bottom")]
    if not values or max(values) - min(values) <= BLOCK_GAP_TOLERANCE_MM:
        return []
    target = sorted(values)[len(values) // 2]
    return [g for g in gaps
            if any(abs(g[s] - target) > BLOCK_GAP_TOLERANCE_MM for s in ("left", "right", "top", "bottom"))]


def text_crossing_blocks(svg: str, tree: list, flip_h: float) -> list[dict]:
    """Texts lying across a block's dashed border."""
    out = []
    for title, block in ed.iter_blocks(tree):
        for t, x0, y0, x1, y1 in svg_text_boxes(svg):
            if _crosses_border((x0 + 0.1, y0 + 0.1, x1 - 0.1, y1 - 0.1), block):
                out.append({"block": title, "text": t,
                            "position_mm": [round((x0 + x1) / 2, 2), round(flip_h - (y0 + y1) / 2, 2)]})
    return out


# --------------------------------------------------------------------------- #
# Crowding
# --------------------------------------------------------------------------- #


def _visible(prop: list) -> bool:
    """False if a property is hidden (`(hide yes)` on the property or in effects)."""
    for node in (prop, find_child(prop, "effects") or []):
        for c in node[1:]:
            if is_call(c, "hide") and (len(c) < 2 or str(c[1]) == "yes"):
                return False
            if isinstance(c, sexpdata.Symbol) and str(c) == "hide":
                return False
    return True


def drawn_references(trees: list[list]) -> Counter:
    """How many symbol instances should draw each visible reference."""
    expected: Counter = Counter()
    for tree in trees:
        for s_node in ed.iter_instance_symbols(tree):
            for prop in find_children(s_node, "property"):
                if len(prop) >= 3 and prop[1] == "Reference" and prop[2] and _visible(prop):
                    expected[prop[2]] += 1
    return expected


def crowded_symbols(svg_texts: list[str], expected: Counter) -> list[str]:
    """References drawn more often than they occur: KiCAD over-plotted them
    because their symbol overlaps another one."""
    drawn: Counter = Counter()
    for svg in svg_texts:
        drawn.update(re.findall(r"<desc>([^<]*)</desc>", svg))
    crowded = []
    for ref, n in expected.items():
        # Multi-unit symbols draw "U1A", "U1B", ...
        count = drawn[ref] + sum(
            c for text, c in drawn.items() if re.fullmatch(re.escape(ref) + r"[A-Z]+", text)
        )
        if count > n:
            crowded.append(ref)
    return sorted(crowded)


def power_attached_references(tree: list) -> set[str]:
    """References of symbols that have a power symbol sitting on one of their pins.

    Such a power symbol always touches the symbol's bounding box, so KiCAD
    over-plots both even though nothing is crowded.
    """
    power_pins: set[tuple[float, float]] = set()
    pins_by_ref: dict[str, list[tuple[float, float]]] = {}
    for s_node in ed.iter_instance_symbols(tree):
        ref = ed.get_symbol_property(s_node, "Reference") or "?"
        lib_id = find_child(s_node, "lib_id")
        sym_def = ed.find_lib_symbol_def(tree, lib_id[1]) if lib_id else None
        try:
            pins = [tuple(p["position_mm"]) for p in ed.list_pins_for_symbol(tree, ref)]
        except (KeyError, ValueError):
            continue
        if sym_def is not None and find_child(sym_def, "power") is not None:
            power_pins.update((round(x, 3), round(y, 3)) for x, y in pins)
        else:
            pins_by_ref[ref] = [(round(x, 3), round(y, 3)) for x, y in pins]
    return {ref for ref, pins in pins_by_ref.items() if any(p in power_pins for p in pins)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def verify(project_dir: Path, project_name: str, *, timeout: float = 120.0) -> dict:
    """Run every check on a project's schematic. Outputs go to `<project>/verify/`."""
    project_dir = Path(project_dir)
    root = project_dir / f"{project_name}.kicad_sch"
    out_dir = project_dir / "verify"
    out_dir.mkdir(exist_ok=True)

    erc = kicad_cli.run_erc(root, timeout=timeout)

    net_path = out_dir / f"{project_name}.net"
    kicad_cli.export_netlist(root, net_path, timeout=timeout)
    nets = parse_netlist(net_path.read_text(encoding="utf-8"))
    single = sorted(n for n, nodes in nets.items() if len(nodes) == 1)

    files = sheet_files(root)
    trees = [sch_io.parse_file(f) for f in files]
    geometry: dict[str, list[dict]] = {"off_grid": [], "outside_frame": [], "on_title_block": []}
    for f, tree in zip(files, trees):
        for k, v in geometry_issues(tree, f.name).items():
            geometry[k] += v

    svg_dir = out_dir / "svg"
    for old in svg_dir.glob("*.svg"):
        old.unlink()
    kicad_cli._run(["sch", "export", "svg", "-o", str(svg_dir), str(root)], timeout=timeout)
    # A power symbol placed on a pin touches that symbol's outline, so KiCAD
    # over-plots the pair; only report symbols without such an explanation.
    attached = set().union(*(power_attached_references(t) for t in trees))
    crowded = [
        ref
        for ref in crowded_symbols(
            [p.read_text(encoding="utf-8") for p in svg_dir.glob("*.svg")],
            drawn_references(trees),
        )
        if ref not in attached
    ]

    texts: dict[str, list[dict]] = {
        "text_outside_frame": [], "text_overlaps": [], "text_on_wires": [], "wires_through_symbols": [],
        "labels_off_wire": [], "text_crossing_blocks": [],
    }
    style: dict[str, list[dict]] = {}
    for tree in trees:
        for k, v in style_issues(tree).items():
            style.setdefault(k, []).extend(v)
    svg_for_sheet = {}
    flip_h = ed.page_height_mm(trees[0])
    for svg_path in sorted(svg_dir.glob("*.svg")):
        svg = svg_path.read_text(encoding="utf-8")
        size = re.search(r'width="([\d.]+)mm" height="([\d.]+)mm"', svg)
        page_w, page_h = (float(size[1]), float(size[2])) if size else ed.page_size_mm(trees[0])
        for k, v in text_issues(svg, page_w, page_h, flip_h).items():
            texts[k] += v
        svg_for_sheet[svg_path] = svg
    # Wiring checks need each page's own wires. The SVG file names don't say
    # which sheet they show, so only single-sheet designs are checked.
    if len(trees) == 1 and len(svg_for_sheet) == 1:
        only_svg = next(iter(svg_for_sheet.values()))
        for k, v in wiring_issues(only_svg, trees[0], flip_h).items():
            texts[k] += v
        texts["text_crossing_blocks"] += text_crossing_blocks(only_svg, trees[0], flip_h)

    pdf = out_dir / f"{project_name}.pdf"
    kicad_cli._run(["sch", "export", "pdf", "-o", str(pdf), str(root)], timeout=timeout)

    problems = []
    if erc["errors"] or erc["warnings"]:
        problems.append(f"ERC: {erc['errors']} errors, {erc['warnings']} warnings")
    for key, label in (
        ("off_grid", "off the 100 mil grid"),
        ("outside_frame", "outside the drawing frame"),
        ("on_title_block", "on the title block"),
    ):
        if geometry[key]:
            items = ", ".join(e["item"] for e in geometry[key][:8])
            problems.append(f"{len(geometry[key])} item(s) {label}: {items}")
    if texts["text_outside_frame"]:
        items = ", ".join(repr(e["text"]) for e in texts["text_outside_frame"][:8])
        problems.append(f"{len(texts['text_outside_frame'])} text(s) crossing the drawing frame: {items}")
    if texts["text_overlaps"]:
        items = ", ".join(" / ".join(repr(t) for t in e["texts"]) for e in texts["text_overlaps"][:8])
        problems.append(f"{len(texts['text_overlaps'])} overlapping text pair(s): {items}")
    if texts["text_on_wires"]:
        items = ", ".join(repr(e["text"]) for e in texts["text_on_wires"][:8])
        problems.append(f"{len(texts['text_on_wires'])} text(s) lying on a wire: {items}")
    if texts["wires_through_symbols"]:
        items = ", ".join(sorted({e["symbol"] for e in texts["wires_through_symbols"]})[:8])
        problems.append(f"{len(texts['wires_through_symbols'])} wire(s) crossing a symbol body: {items}")
    if texts["labels_off_wire"]:
        items = ", ".join(repr(e["label"]) for e in texts["labels_off_wire"][:8])
        problems.append(f"{len(texts['labels_off_wire'])} label(s) running past the end of their wire: {items}")
    if crowded:
        problems.append(
            f"crowded symbols (overlapping another symbol or its text): {', '.join(crowded)}"
        )
    if texts["text_crossing_blocks"]:
        items = ", ".join(repr(e["text"]) for e in texts["text_crossing_blocks"][:8])
        problems.append(f"{len(texts['text_crossing_blocks'])} text(s) across a block border: {items}")
    if style["blocks_overlapping"]:
        items = ", ".join(" / ".join(e["blocks"]) for e in style["blocks_overlapping"][:8])
        problems.append(f"overlapping blocks: {items}")
    if style["block_spacing"]:
        items = ", ".join(
            f"{e['block']} (L {e['left']}, R {e['right']}, T {e['top']}, B {e['bottom']} mm)"
            for e in style["block_spacing"][:6])
        problems.append("blocks without equal gaps to each other, the title block and the frame: " + items)
    if style["crossing_blocks"]:
        items = ", ".join(sorted({f"{e['item']} ({e['block']})" for e in style["crossing_blocks"]})[:8])
        problems.append(f"{len(style['crossing_blocks'])} wire(s)/symbol(s) across a block border "
                        f"(connect blocks with labels): {items}")
    if style["long_wires"]:
        items = ", ".join(f"{'/'.join(e['nets']) or '?'} {e['span_mm']} mm" for e in style["long_wires"][:8])
        problems.append(f"{len(style['long_wires'])} wire run(s) longer than a quarter of the sheet width "
                        f"({style['long_wires'][0]['limit_mm']} mm; use net labels at both ends): {items}")
    if style["uncolored_power_wires"]:
        items = ", ".join("/".join(e["nets"]) for e in style["uncolored_power_wires"][:8])
        problems.append(f"power wires not red (run color_power_wires): {items}")
    if style["lone_test_points"]:
        items = ", ".join(e["reference"] for e in style["lone_test_points"][:8])
        problems.append(f"test point(s) not wired into the circuit: {items}")
    if style["long_values"]:
        items = ", ".join(f"{e['reference']} {e['value']!r}" for e in style["long_values"][:8])
        problems.append(f"value(s) longer than a short value (move the rest to fields): {items}")
    if style["ref_value_layout"]:
        items = ", ".join(e["reference"] for e in style["ref_value_layout"][:8])
        problems.append(f"Reference/Value not stacked consistently: {items}")

    return {
        "ok": not problems,
        "problems": problems,
        "erc": {k: erc[k] for k in ("errors", "warnings", "violations", "raw_path")},
        "nets": nets,
        "single_connection_nets": single,
        **geometry,
        "crowded_symbols": crowded,
        **texts,
        **style,
        "sheets": [f.name for f in files],
        "pdf": str(pdf),
        "netlist": str(net_path),
        "note": "Open the PDF and look at it: these checks cannot judge readability "
        "or whether the circuit does what was asked.",
    }
