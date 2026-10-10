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

# KiCAD's default title block, anchored at the frame's bottom-right corner.
TITLE_BLOCK_W_MM = 110.0
TITLE_BLOCK_H_MM = 34.0


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
        "labels_off_wire": [],
    }
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
        for k, v in wiring_issues(next(iter(svg_for_sheet.values())), trees[0], flip_h).items():
            texts[k] += v

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

    return {
        "ok": not problems,
        "problems": problems,
        "erc": {k: erc[k] for k in ("errors", "warnings", "violations", "raw_path")},
        "nets": nets,
        "single_connection_nets": single,
        **geometry,
        "crowded_symbols": crowded,
        **texts,
        "sheets": [f.name for f in files],
        "pdf": str(pdf),
        "netlist": str(net_path),
        "note": "Open the PDF and look at it: these checks cannot judge readability "
        "or whether the circuit does what was asked.",
    }
