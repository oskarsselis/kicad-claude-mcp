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
        x0, y0, x1, y1 = ed.symbol_outline(sym_def, float(at[1]), float(at[2]), int(float(at[3])))[0]
        boxes.append((ref, x0, y0, x1, y1))
    boxes += [(what, x, y, x, y) for what, x, y in points if " pin " not in what]

    for what, x0, y0, x1, y1 in boxes:
        if x0 < m or y0 < m or x1 > w - m or y1 > h - m:
            outside.append(entry(what, (x0 + x1) / 2, (y0 + y1) / 2))
        elif x1 > tb[0] and y1 > tb[1]:
            on_title.append(entry(what, (x0 + x1) / 2, (y0 + y1) / 2))
    return {"off_grid": off_grid, "outside_frame": outside, "on_title_block": on_title}


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
    crowded = crowded_symbols(
        [p.read_text(encoding="utf-8") for p in svg_dir.glob("*.svg")], drawn_references(trees)
    )

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
        "sheets": [f.name for f in files],
        "pdf": str(pdf),
        "netlist": str(net_path),
        "note": "Open the PDF and look at it: these checks cannot judge readability "
        "or whether the circuit does what was asked.",
    }
