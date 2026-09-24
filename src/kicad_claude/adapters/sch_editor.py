"""High-level mutations on a parsed `.kicad_sch` tree.

Operates on raw sexpdata trees — `parse_file` from `sch_io.py` returns the
top-level list, and these helpers mutate it in place. Use `write_file` to
serialize back.

Coordinate convention: all `x_mm`/`y_mm` parameters are in **MCP coordinates**
(Y up). Internal storage is in KiCAD coordinates (Y down). The `_mcp_to_at`
helper does the conversion at the boundary.
"""

from __future__ import annotations

import copy
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import sexpdata

from kicad_claude.adapters import sch_io
from kicad_claude.adapters.sch_io import (
    find_child,
    find_children,
    head_of,
    is_call,
    sym,
)
from kicad_claude.utils.geometry import (
    mcp_to_kicad_xy,
    normalize_rotation,
    rotate_xy,
    round_mm,
)

# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


def backup_file(path: Path) -> Path | None:
    """Copy `path` to `<project>/.backups/<timestamp>_<filename>`. Idempotent if file missing."""
    path = Path(path)
    if not path.is_file():
        return None
    backups = path.parent / ".backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups / f"{stamp}_{path.name}"
    shutil.copy2(path, dest)
    return dest


# --------------------------------------------------------------------------- #
# Page height (Y-flip parameter)
# --------------------------------------------------------------------------- #


# Schematic connection grid: 100 mil. Wire ends, pin ends and labels belong on it.
SCH_GRID_MM = 2.54

# Distance from the paper edge to KiCAD's default drawing-sheet frame.
SHEET_FRAME_MARGIN_MM = 10.0

# KiCAD paper sizes, landscape (width, height) in mm.
_PAPER_SIZES_MM = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
    "A5": (210.0, 148.0),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}


def page_size_mm(tree: list) -> tuple[float, float]:
    """Paper (width, height) in mm from `(paper ...)`. Defaults to A4 landscape."""
    paper = find_child(tree, "paper")
    if not (paper and len(paper) >= 2 and isinstance(paper[1], str)):
        return _PAPER_SIZES_MM["A4"]
    if paper[1] == "User" and len(paper) >= 4:
        return float(paper[2]), float(paper[3])
    w, h = _PAPER_SIZES_MM.get(paper[1], _PAPER_SIZES_MM["A4"])
    if any(isinstance(p, sexpdata.Symbol) and str(p) == "portrait" for p in paper[2:]):
        w, h = h, w
    return w, h


def page_height_mm(tree: list) -> float:
    """Y-flip reference for MCP <-> KiCAD coordinates.

    This is the paper height rounded down to the connection grid, not the raw
    height: A4's 210 mm is not a multiple of 2.54, and flipping around it
    would push every on-grid MCP coordinate off the grid in the file.
    """
    _, h = page_size_mm(tree)
    return round_mm(int(h / SCH_GRID_MM) * SCH_GRID_MM)


def require_inside_frame(tree: list, x_k: float, y_k: float, what: str) -> None:
    """Refuse a point (KiCAD coords) that falls outside the drawing-sheet frame."""
    w, h = page_size_mm(tree)
    m = SHEET_FRAME_MARGIN_MM
    if not (m <= x_k <= w - m and m <= y_k <= h - m):
        page_h = page_height_mm(tree)
        raise ValueError(
            f"{what}: MCP point ({round_mm(x_k)}, {round_mm(page_h - y_k)}) is outside "
            f"the drawing frame; valid MCP range is x {m}..{w - m}, "
            f"y {round_mm(page_h - (h - m))}..{round_mm(page_h - m)} (Y up)"
        )


def _snap(v: float) -> float:
    return round_mm(round(v / SCH_GRID_MM) * SCH_GRID_MM)


def _on_grid(v: float) -> bool:
    return abs(v - _snap(v)) < 1e-3


def require_on_grid(x_k: float, y_k: float, what: str, page_h: float) -> None:
    """Refuse a connection point (KiCAD coords) that is off the 100 mil grid."""
    if _on_grid(x_k) and _on_grid(y_k):
        return
    raise ValueError(
        f"{what}: MCP point ({round_mm(x_k)}, {round_mm(page_h - y_k)}) is off the "
        f"100 mil (2.54 mm) grid; nearest on-grid point is "
        f"({_snap(x_k)}, {round_mm(page_h - _snap(y_k))})"
    )


def require_pins_on_grid(
    sym_def: list, x_k: float, y_k: float, rot: int, reference: str, page_h: float
) -> None:
    """Refuse a symbol placement whose visible pin ends miss the 100 mil grid.

    The symbol origin itself may be off grid (Device:R pins are 150 mil from
    its centre); the error names the nearest origin that puts the pins on it.
    """
    pin_ends = symbol_outline(sym_def, x_k, y_k, rot)[1]
    if all(_on_grid(px) and _on_grid(py) for px, py in pin_ends):
        return
    px, py = pin_ends[0]
    dx, dy = _snap(px) - px, _snap(py) - py
    if all(_on_grid(qx + dx) and _on_grid(qy + dy) for qx, qy in pin_ends):
        hint = (
            f"place it at MCP ({round_mm(x_k + dx)}, {round_mm(page_h - (y_k + dy))}) "
            f"instead"
        )
    else:
        hint = "its pins are not on a common 100 mil grid in the library"
    raise ValueError(f"symbol {reference}: pin ends are off the 100 mil grid; {hint}")


# --------------------------------------------------------------------------- #
# Symbol instance lookup
# --------------------------------------------------------------------------- #


def iter_instance_symbols(tree: list):
    """Yield top-level (symbol ...) nodes that are instances (skip lib_symbols container)."""
    for child in tree[1:]:
        if is_call(child, "symbol") and find_child(child, "lib_id"):
            yield child


def get_symbol_property(symbol_node: list, name: str) -> str | None:
    for prop in find_children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name and isinstance(prop[2], str):
            return prop[2]
    return None


def set_symbol_property(symbol_node: list, name: str, value: str) -> None:
    for prop in find_children(symbol_node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            prop[2] = value
            return
    raise KeyError(f"property {name!r} not found on symbol")


def find_symbol_by_reference(tree: list, reference: str) -> list | None:
    for s_node in iter_instance_symbols(tree):
        if get_symbol_property(s_node, "Reference") == reference:
            return s_node
    return None


def all_references(tree: list) -> list[str]:
    return [get_symbol_property(s, "Reference") or "?" for s in iter_instance_symbols(tree)]


# --------------------------------------------------------------------------- #
# lib_symbols injection
# --------------------------------------------------------------------------- #


def get_or_create_lib_symbols(tree: list) -> list:
    block = find_child(tree, "lib_symbols")
    if block is not None:
        return block
    # Insert after (paper ...) for natural ordering.
    new = [sym("lib_symbols")]
    insert_at = 1
    for i, child in enumerate(tree[1:], start=1):
        if is_call(child, "paper"):
            insert_at = i + 1
            break
    tree.insert(insert_at, new)
    return new


def lib_symbols_has(tree: list, qualified_lib_id: str) -> bool:
    block = find_child(tree, "lib_symbols")
    if not block:
        return False
    for child in block[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == qualified_lib_id:
            return True
    return False


def inject_lib_symbol(tree: list, symbol_def_node: list) -> None:
    """Append a fully-qualified lib_symbols entry. Idempotent on lib_id."""
    block = get_or_create_lib_symbols(tree)
    qualified = symbol_def_node[1] if len(symbol_def_node) >= 2 else None
    if qualified and lib_symbols_has(tree, qualified):
        return
    block.append(symbol_def_node)


# --------------------------------------------------------------------------- #
# Symbol creation (from a lib def + placement parameters)
# --------------------------------------------------------------------------- #


def fetch_symbol_def(lib_path: Path, symbol_name: str) -> list:
    """Open a `.kicad_sym`, return a deep copy of the named (symbol ...) node.

    Derived symbols (`(extends "Base")`, e.g. Transistor_FET:2N7002) are
    flattened the way KiCAD embeds them in a schematic: the base's graphics,
    pins and flags, with the derived symbol's properties taking precedence.
    The returned node is renamed-ready for lib_symbols: the caller should
    set its name to "LibName:SymbolName" before injecting.
    """
    text = Path(lib_path).read_text(encoding="utf-8", errors="replace")
    data = sexpdata.loads(text)
    if not is_call(data, "kicad_symbol_lib"):
        raise ValueError(f"not a kicad_symbol_lib: {lib_path}")
    by_name = {
        child[1]: child
        for child in data[1:]
        if is_call(child, "symbol") and len(child) >= 2 and isinstance(child[1], str)
    }
    if symbol_name not in by_name:
        raise KeyError(f"symbol {symbol_name!r} not found in {lib_path}")
    return _flatten_symbol(by_name, symbol_name, seen=set())


def _flatten_symbol(by_name: dict[str, list], name: str, seen: set[str]) -> list:
    node = copy.deepcopy(by_name[name])
    ext = find_child(node, "extends")
    if ext is None:
        return node
    base_name = ext[1]
    if base_name in seen or base_name not in by_name:
        raise KeyError(f"symbol {name!r} extends unknown or cyclic base {base_name!r}")
    base = _flatten_symbol(by_name, base_name, seen | {name})

    own_props = {p[1]: p for p in find_children(node, "property") if len(p) >= 3}
    out: list[Any] = [sym("symbol"), name]
    for child in base[2:]:
        if is_call(child, "property") and len(child) >= 3 and child[1] in own_props:
            out.append(own_props.pop(child[1]))
        elif head_of(child) == "symbol" and isinstance(child[1], str):
            # Unit sub-symbols are named after their parent: Base_1_1 -> Name_1_1.
            child[1] = name + child[1][len(base_name):]
            out.append(child)
        else:
            out.append(child)
    # Derived-only properties go after the inherited ones, before the units.
    first_unit = next(
        (i for i, c in enumerate(out) if head_of(c) == "symbol"), len(out)
    )
    out[first_unit:first_unit] = list(own_props.values())
    return out


def make_lib_symbol_entry(symbol_def_node: list, qualified_lib_id: str) -> list:
    """Rename the symbol def's name to `Lib:Name` for lib_symbols use."""
    symbol_def_node[1] = qualified_lib_id
    return symbol_def_node


def collect_pin_numbers(symbol_def_node: list) -> list[str]:
    """Return pin number strings from a lib symbol definition."""
    pins: list[str] = []
    for child in symbol_def_node[2:]:
        h = head_of(child)
        if h == "pin":
            for sub in child[1:]:
                if is_call(sub, "number") and len(sub) >= 2 and isinstance(sub[1], str):
                    pins.append(sub[1])
        elif h == "symbol":
            pins.extend(collect_pin_numbers(child))
    return pins


# --------------------------------------------------------------------------- #
# Symbol outline + field placement
# --------------------------------------------------------------------------- #

FIELD_TEXT_MM = 1.27  # KiCAD default field font size
FIELD_GAP_MM = 1.27  # clearance between the symbol outline and its field text
FIELD_LINE_MM = 2.54  # spacing between the stacked Reference / Value lines


def _is_hidden(node: list) -> bool:
    """True for `(... hide ...)` (KiCAD <= 7) or `(... (hide yes) ...)` (8+)."""
    for c in node[1:]:
        if isinstance(c, sexpdata.Symbol) and str(c) == "hide":
            return True
        if is_call(c, "hide") and (len(c) < 2 or str(c[1]) == "yes"):
            return True
    return False


def _drawn_items(symbol_def_node: list):
    """Yield graphic/pin nodes drawn for unit 1, body style 1 (plus shared unit 0)."""
    for child in symbol_def_node[2:]:
        if head_of(child) == "symbol" and len(child) >= 2 and isinstance(child[1], str):
            parts = child[1].rsplit("_", 2)
            unit, style = (parts[1], parts[2]) if len(parts) == 3 else ("0", "0")
            if unit in ("0", "1") and style in ("0", "1"):
                yield from child[2:]
        else:
            yield child


def _xy(node: list | None) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    return float(node[1]), float(node[2])


def symbol_outline(
    symbol_def_node: list, sx: float, sy: float, rot: int
) -> tuple[tuple[float, float, float, float], list[tuple[float, float]]]:
    """Outline of a placed symbol in KiCAD file coords (Y down).

    Returns `(bbox, pin_ends)`: bbox = (x0, y0, x1, y1) around the body
    graphics and pins, pin_ends = connection points of the visible pins.
    """
    body: list[tuple[float, float]] = []
    pin_ends: list[tuple[float, float]] = []
    for item in _drawn_items(symbol_def_node):
        h = head_of(item)
        if h == "rectangle":
            body += [p for p in (_xy(find_child(item, "start")), _xy(find_child(item, "end"))) if p]
        elif h in ("polyline", "bezier"):
            pts = find_child(item, "pts")
            body += [p for p in (_xy(c) for c in find_children(pts or [], "xy")) if p]
        elif h == "arc":
            body += [p for p in (_xy(find_child(item, k)) for k in ("start", "mid", "end")) if p]
        elif h == "circle":
            c = _xy(find_child(item, "center"))
            r = find_child(item, "radius")
            if c and r:
                rr = float(r[1])
                body += [(c[0] - rr, c[1] - rr), (c[0] + rr, c[1] + rr)]
        elif h == "pin" and not _is_hidden(item):
            px, py, angle = _pin_local_at(item)
            length_node = find_child(item, "length")
            length = float(length_node[1]) if length_node else 0.0
            dx, dy = rotate_xy(length, 0.0, angle)
            pin_ends.append((px, py))
            body.append((px + dx, py + dy))

    def to_file(p: tuple[float, float]) -> tuple[float, float]:
        rx, ry = rotate_xy(p[0], p[1], rot)  # library coords are Y up
        return sx + rx, sy - ry

    body_f = [to_file(p) for p in body] or [(sx, sy)]
    ends_f = [to_file(p) for p in pin_ends]
    xs = [p[0] for p in body_f + ends_f]
    ys = [p[1] for p in body_f + ends_f]
    return (min(xs), min(ys), max(xs), max(ys)), ends_f


def _field_sides(symbol_def_node: list, sx: float, sy: float, rot: int):
    """Pick the side of the symbol for its Reference/Value text: the first of
    right, left, top, bottom that has no pins. Returns (side, bbox)."""
    bbox, pin_ends = symbol_outline(symbol_def_node, sx, sy, rot)
    x0, y0, x1, y1 = bbox
    eps = 1e-6
    used = set()
    for px, py in pin_ends:
        if px >= x1 - eps and not (py <= y0 + eps or py >= y1 - eps):
            used.add("right")
        if px <= x0 + eps and not (py <= y0 + eps or py >= y1 - eps):
            used.add("left")
        if py <= y0 + eps:
            used.add("top")
        if py >= y1 - eps:
            used.add("bottom")
    for side in ("right", "left", "top", "bottom"):
        if side not in used:
            return side, bbox
    return "top", bbox


def place_ref_value(
    symbol_def_node: list, sx: float, sy: float, rot: int
) -> tuple[tuple[float, float, str], tuple[float, float, str], int]:
    """Anchor points for Reference and Value, stacked on two lines beside the
    symbol and clear of its outline.

    Returns ((ref_x, ref_y, justify), (val_x, val_y, justify), field_angle),
    in file coords. Field angle and justification are stored relative to the
    symbol's orientation in KiCAD, so they are chosen here such that the text
    reads horizontally and grows away from the symbol at any rotation.
    """
    side, (x0, y0, x1, y1) = _field_sides(symbol_def_node, sx, sy, rot)
    half = FIELD_TEXT_MM / 2
    cy = (y0 + y1) / 2
    if side == "right":
        x, grow = x1 + FIELD_GAP_MM, "left"
        ref_y, val_y = cy - FIELD_LINE_MM / 2, cy + FIELD_LINE_MM / 2
    elif side == "left":
        x, grow = x0 - FIELD_GAP_MM, "right"
        ref_y, val_y = cy - FIELD_LINE_MM / 2, cy + FIELD_LINE_MM / 2
    elif side == "top":
        x, grow = x0, "left"
        val_y = y0 - FIELD_GAP_MM - half
        ref_y = val_y - FIELD_LINE_MM
    else:
        x, grow = x0, "left"
        ref_y = y1 + FIELD_GAP_MM + half
        val_y = ref_y + FIELD_LINE_MM
    # Rotation 90/180 mirrors the stored justification on screen.
    flip = {"left": "right", "right": "left"}
    justify = flip[grow] if rot in (90, 180) else grow
    angle = 90 if rot in (90, 270) else 0
    return (
        (round_mm(x), round_mm(ref_y), justify),
        (round_mm(x), round_mm(val_y), justify),
        angle,
    )


def _lib_property(symbol_def_node: list, name: str) -> list | None:
    for prop in find_children(symbol_def_node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            return prop
    return None


def _power_value_prop(
    symbol_def_node: list, value: str, x_k: float, y_k: float, rot: int
) -> list:
    """Value field of a power symbol, positioned and styled as in its library."""
    lib = _lib_property(symbol_def_node, "Value")
    lib_at = find_child(lib, "at") if lib else None
    lx, ly, langle = 0.0, 0.0, 0.0
    if lib_at and len(lib_at) >= 4:
        lx, ly, langle = float(lib_at[1]), float(lib_at[2]), float(lib_at[3])
    rx, ry = rotate_xy(lx, ly, rot)
    effects = copy.deepcopy(find_child(lib, "effects")) if lib else None
    if effects is None:
        effects = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
    effects[1:] = [c for c in effects[1:] if not is_call(c, "hide")]
    return [
        sym("property"), "Value", value,
        [sym("at"), round_mm(x_k + rx), round_mm(y_k - ry), langle],
        effects,
    ]


def build_symbol_instance(
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mcp: float,
    y_mcp: float,
    rotation_deg: int,
    pin_numbers: list[str],
    project_name: str,
    instance_path: str,
    page_h: float,
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
    sym_def_node: list | None = None,
) -> list:
    """Construct a new (symbol ...) instance node ready to inject into the schematic.

    With `sym_def_node`, Reference and Value are laid out beside the symbol
    (see `place_ref_value`); power symbols keep the library's Value position
    and hide their `#PWR` reference. Footprint/Datasheet/Description are
    hidden metadata anchored at the symbol origin.
    """
    x_k, y_k = mcp_to_kicad_xy(x_mcp, y_mcp, page_h)
    x_k, y_k = round_mm(x_k), round_mm(y_k)

    inst_uuid = str(uuid.uuid4())

    def _prop(
        name: str,
        val: str,
        hide: bool,
        at: tuple[float, float, float] = (x_k, y_k, 0),
        justify: list | None = None,
    ) -> list:
        node: list[Any] = [sym("property"), name, val, [sym("at"), *at]]
        effects: list[Any] = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]]]
        if justify:
            effects.append([sym("justify"), *justify])
        if hide:
            effects.append([sym("hide"), sym("yes")])
        node.append(effects)
        return node

    is_power = sym_def_node is not None and find_child(sym_def_node, "power") is not None
    if sym_def_node is None:
        ref_prop = _prop("Reference", reference, hide=False)
        val_prop = _prop("Value", value, hide=False)
    elif is_power:
        ref_prop = _prop("Reference", reference, hide=True)
        val_prop = _power_value_prop(sym_def_node, value, x_k, y_k, rotation_deg)
    else:
        ref_at, val_at, angle = place_ref_value(sym_def_node, x_k, y_k, rotation_deg)
        ref_prop = _prop(
            "Reference", reference, hide=False,
            at=(ref_at[0], ref_at[1], angle), justify=[sym(ref_at[2])],
        )
        val_prop = _prop(
            "Value", value, hide=False,
            at=(val_at[0], val_at[1], angle), justify=[sym(val_at[2])],
        )

    pin_nodes = [
        [sym("pin"), num, [sym("uuid"), str(uuid.uuid4())]]
        for num in pin_numbers
    ]

    instances_node = [
        sym("instances"),
        [
            sym("project"),
            project_name,
            [
                sym("path"),
                instance_path,
                [sym("reference"), reference],
                [sym("unit"), 1],
            ],
        ],
    ]

    return [
        sym("symbol"),
        [sym("lib_id"), qualified_lib_id],
        [sym("at"), x_k, y_k, rotation_deg],
        [sym("unit"), 1],
        [sym("exclude_from_sim"), sym("no")],
        [sym("in_bom"), sym("yes")],
        [sym("on_board"), sym("yes")],
        [sym("dnp"), sym("no")],
        [sym("uuid"), inst_uuid],
        ref_prop,
        val_prop,
        _prop("Footprint", footprint, hide=True),
        _prop("Datasheet", datasheet, hide=True),
        _prop("Description", description, hide=True),
        *pin_nodes,
        instances_node,
    ]


# --------------------------------------------------------------------------- #
# Public mutations
# --------------------------------------------------------------------------- #


def add_symbol(
    tree: list,
    *,
    qualified_lib_id: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    rotation: float,
    sym_def_node: list,
    project_name: str,
    instance_path: str | None = None,
    footprint: str = "",
    datasheet: str = "~",
    description: str = "",
) -> list:
    """Inject a symbol into the schematic. Returns the new (symbol ...) node.

    `instance_path` is the KiCAD instance path (e.g. `/<root_uuid>` for the
    root sheet, `/<root_uuid>/<sheet_uuid>` for a child). If None, defaults
    to `/<this_file's_uuid>` — only correct for non-hierarchical use.

    Unannotated references (ending in `?`, e.g. `R?`) are explicitly allowed
    to repeat — they're meant to be resolved later by `annotate_schematic`.
    """
    if not reference.endswith("?") and find_symbol_by_reference(tree, reference) is not None:
        raise ValueError(f"reference {reference!r} already exists")

    rot = normalize_rotation(rotation)
    page_h = page_height_mm(tree)
    if instance_path is None:
        instance_path = f"/{_schematic_uuid(tree)}"

    lib_entry_def = make_lib_symbol_entry(sym_def_node, qualified_lib_id)
    x0, y0, x1, y1 = symbol_outline(lib_entry_def, *mcp_to_kicad_xy(x_mm, y_mm, page_h), rot)[0]
    for corner in ((x0, y0), (x1, y1)):
        require_inside_frame(tree, *corner, f"symbol {reference}")
    require_pins_on_grid(
        lib_entry_def, *mcp_to_kicad_xy(x_mm, y_mm, page_h), rot, reference, page_h
    )

    # Inject the lib symbol definition (idempotent on qualified id).
    inject_lib_symbol(tree, lib_entry_def)

    pins = collect_pin_numbers(lib_entry_def)
    instance = build_symbol_instance(
        qualified_lib_id=qualified_lib_id,
        reference=reference,
        value=value,
        x_mcp=x_mm,
        y_mcp=y_mm,
        rotation_deg=rot,
        pin_numbers=pins,
        project_name=project_name,
        instance_path=instance_path,
        page_h=page_h,
        footprint=footprint,
        datasheet=datasheet,
        description=description,
        sym_def_node=lib_entry_def,
    )
    tree.append(instance)
    return instance


def remove_symbol(tree: list, reference: str) -> bool:
    """Remove the first symbol matching `reference`. Returns True if removed."""
    for i, child in enumerate(tree):
        if (
            is_call(child, "symbol")
            and find_child(child, "lib_id")
            and get_symbol_property(child, "Reference") == reference
        ):
            tree.pop(i)
            return True
    return False


def move_symbol(
    tree: list,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation: float | None = None,
) -> None:
    """Set absolute position (and optionally rotation) of an existing symbol.

    Field positions are absolute in the file, so they move along: Reference
    and Value are laid out again for the new placement, the other fields
    shift by the same offset as the symbol.
    """
    s_node = find_symbol_by_reference(tree, reference)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    page_h = page_height_mm(tree)
    x_k, y_k = (round_mm(v) for v in mcp_to_kicad_xy(x_mm, y_mm, page_h))
    at = find_child(s_node, "at")
    if at is None or len(at) < 4:
        raise ValueError("symbol has malformed (at ...) node")
    rot = normalize_rotation(rotation) if rotation is not None else int(float(at[3]))
    lib_id = find_child(s_node, "lib_id")
    sym_def = find_lib_symbol_def(tree, lib_id[1]) if lib_id and len(lib_id) >= 2 else None
    if sym_def is not None:
        x0, y0, x1, y1 = symbol_outline(sym_def, x_k, y_k, rot)[0]
        for corner in ((x0, y0), (x1, y1)):
            require_inside_frame(tree, *corner, f"symbol {reference}")
        require_pins_on_grid(sym_def, x_k, y_k, rot, reference, page_h)

    dx, dy = x_k - float(at[1]), y_k - float(at[2])
    at[1], at[2], at[3] = x_k, y_k, rot
    for prop in find_children(s_node, "property"):
        p_at = find_child(prop, "at")
        if p_at is not None and len(p_at) >= 3:
            p_at[1] = round_mm(float(p_at[1]) + dx)
            p_at[2] = round_mm(float(p_at[2]) + dy)
    if sym_def is not None:
        _relayout_ref_value(s_node, sym_def, x_k, y_k, rot)


def _relayout_ref_value(s_node: list, sym_def: list, x_k: float, y_k: float, rot: int) -> None:
    """Re-place an instance's Reference/Value the way `build_symbol_instance` does."""
    props = {p[1]: p for p in find_children(s_node, "property") if len(p) >= 3}
    if find_child(sym_def, "power") is not None:
        if "Value" in props:
            fresh = _power_value_prop(sym_def, props["Value"][2], x_k, y_k, rot)
            props["Value"][3:] = fresh[3:]
        return
    ref_at, val_at, angle = place_ref_value(sym_def, x_k, y_k, rot)
    for name, (fx, fy, justify) in (("Reference", ref_at), ("Value", val_at)):
        prop = props.get(name)
        if prop is None:
            continue
        p_at = find_child(prop, "at")
        if p_at is not None:
            p_at[1:] = [fx, fy, angle]
        effects = find_child(prop, "effects")
        if effects is not None:
            effects[1:] = [c for c in effects[1:] if not is_call(c, "justify")]
            effects.insert(2, [sym("justify"), sym(justify)])


def add_wire(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
) -> list:
    """Append a (wire ...) segment between two MCP-coord points. Returns the new node.

    Adds junctions where the new wire makes three or more connections meet at
    one point (e.g. a T onto the middle of an existing wire). KiCAD only joins
    a wire end to the interior of another wire through a junction.
    """
    page_h = page_height_mm(tree)
    x1k, y1k = (round_mm(v) for v in mcp_to_kicad_xy(x1_mm, y1_mm, page_h))
    x2k, y2k = (round_mm(v) for v in mcp_to_kicad_xy(x2_mm, y2_mm, page_h))
    require_inside_frame(tree, x1k, y1k, "wire start")
    require_inside_frame(tree, x2k, y2k, "wire end")
    require_on_grid(x1k, y1k, "wire start", page_h)
    require_on_grid(x2k, y2k, "wire end", page_h)
    node = [
        sym("wire"),
        [
            sym("pts"),
            [sym("xy"), x1k, y1k],
            [sym("xy"), x2k, y2k],
        ],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    _add_needed_junctions(tree, node)
    return node


def add_junction(tree: list, x_k: float, y_k: float) -> list:
    """Append a (junction ...) at a point in KiCAD file coords."""
    node = [
        sym("junction"),
        [sym("at"), round_mm(x_k), round_mm(y_k)],
        [sym("diameter"), 0],
        [sym("color"), 0, 0, 0, 0],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def _wire_ends(wire: list) -> tuple[tuple[float, float], tuple[float, float]] | None:
    pts = find_child(wire, "pts")
    xys = [_xy(c) for c in find_children(pts or [], "xy")]
    if len(xys) < 2 or None in xys[:2]:
        return None
    return xys[0], xys[1]  # type: ignore[return-value]


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-4 and abs(a[1] - b[1]) < 1e-4


def _strictly_inside(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True if `p` lies on segment a-b but is not one of its ends."""
    if _same(p, a) or _same(p, b):
        return False
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > 1e-4:
        return False
    return (
        min(a[0], b[0]) - 1e-4 <= p[0] <= max(a[0], b[0]) + 1e-4
        and min(a[1], b[1]) - 1e-4 <= p[1] <= max(a[1], b[1]) + 1e-4
    )


def _pin_points_file(tree: list) -> list[tuple[float, float]]:
    """Connection points of every placed symbol's pins, in KiCAD file coords."""
    page_h = page_height_mm(tree)
    out = []
    for s_node in iter_instance_symbols(tree):
        ref = get_symbol_property(s_node, "Reference")
        if not ref:
            continue
        try:
            pins = list_pins_for_symbol(tree, ref)
        except (KeyError, ValueError):
            continue
        out += [(p["position_mm"][0], page_h - p["position_mm"][1]) for p in pins]
    return out


def _add_needed_junctions(tree: list, new_wire: list) -> None:
    ends = _wire_ends(new_wire)
    if ends is None:
        return
    wires = [w for w in (_wire_ends(c) for c in tree[1:] if is_call(c, "wire")) if w]
    junctions = [_xy(find_child(c, "at")) for c in tree[1:] if is_call(c, "junction")]
    pins = _pin_points_file(tree)
    candidates = list(ends) + [
        p for p in [e for w in wires for e in w] + pins if _strictly_inside(p, *ends)
    ]
    for p in candidates:
        if any(j and _same(p, j) for j in junctions):
            continue
        links = sum(1 for w in wires for e in w if _same(p, e))
        links += sum(2 for w in wires if _strictly_inside(p, *w))
        links += sum(1 for pin in pins if _same(p, pin))
        if links >= 3:
            add_junction(tree, *p)
            junctions.append(p)


def add_label(
    tree: list,
    net_name: str,
    x_mm: float,
    y_mm: float,
    orientation: str = "right",
) -> list:
    """Append a (label ...) at a point. orientation ∈ {right, up, left, down}."""
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    require_inside_frame(tree, xk, yk, f"label {net_name}")
    require_on_grid(xk, yk, f"label {net_name}", page_h)
    angle_map = {"right": 0, "up": 90, "left": 180, "down": 270}
    if orientation not in angle_map:
        raise ValueError(f"orientation must be one of {list(angle_map)}")
    node = [
        sym("label"),
        net_name,
        [sym("at"), round_mm(xk), round_mm(yk), angle_map[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("left"), sym("bottom")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_no_connect(tree: list, x_mm: float, y_mm: float) -> list:
    """Append a (no_connect ...) marker at a point."""
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    require_on_grid(xk, yk, "no-connect", page_h)
    node = [
        sym("no_connect"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# Buses (visual grouping of multiple nets)
# --------------------------------------------------------------------------- #


def add_bus_segment(
    tree: list,
    x1_mm: float,
    y1_mm: float,
    x2_mm: float,
    y2_mm: float,
) -> list:
    """Append a `(bus ...)` line — visually identical to a wire but thicker."""
    page_h = page_height_mm(tree)
    x1k, y1k = mcp_to_kicad_xy(x1_mm, y1_mm, page_h)
    x2k, y2k = mcp_to_kicad_xy(x2_mm, y2_mm, page_h)
    require_on_grid(x1k, y1k, "bus start", page_h)
    require_on_grid(x2k, y2k, "bus end", page_h)
    node = [
        sym("bus"),
        [
            sym("pts"),
            [sym("xy"), round_mm(x1k), round_mm(y1k)],
            [sym("xy"), round_mm(x2k), round_mm(y2k)],
        ],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_bus_entry(
    tree: list,
    x_mm: float,
    y_mm: float,
    direction: str = "right_down",
) -> list:
    """Append a `(bus_entry ...)` — the diagonal line connecting a bus to a wire.

    `direction` controls the size vector (the offset from `at` to the wire side):
        "right_down" → (+2.54, +2.54)  (default; bus above-left, wire below-right)
        "right_up"   → (+2.54, -2.54)
        "left_down"  → (-2.54, +2.54)
        "left_up"    → (-2.54, -2.54)
    """
    deltas = {
        "right_down": (2.54, 2.54),
        "right_up": (2.54, -2.54),
        "left_down": (-2.54, 2.54),
        "left_up": (-2.54, -2.54),
    }
    if direction not in deltas:
        raise ValueError(
            f"direction must be one of {list(deltas)} (got {direction!r})"
        )
    dx, dy = deltas[direction]
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    require_on_grid(xk, yk, "bus entry", page_h)
    node = [
        sym("bus_entry"),
        [sym("at"), round_mm(xk), round_mm(yk)],
        [sym("size"), dx, dy],
        [sym("stroke"), [sym("width"), 0], [sym("type"), sym("default")]],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_bus_alias(tree: list, alias_name: str, members: list[str]) -> list:
    """Declare `(bus_alias "NAME" (members "M0" "M1" ...))`.

    Aliases let you label a bus with a short name on the wire (e.g. "MEM")
    instead of writing out `MEM[0..7]`. Members must be exact net names.
    """
    if not alias_name:
        raise ValueError("alias_name must be non-empty")
    if not members:
        raise ValueError("alias needs at least one member net")
    members_node: list = [sym("members")]
    for m in members:
        members_node.append(str(m))
    node = [sym("bus_alias"), alias_name, members_node]
    tree.append(node)
    return node


# --------------------------------------------------------------------------- #
# Hierarchical sheets
# --------------------------------------------------------------------------- #


_LABEL_ORIENTATIONS = {"right": 0, "up": 90, "left": 180, "down": 270}
_VALID_LABEL_SHAPES = {"input", "output", "bidirectional", "tri_state", "passive"}


def add_sheet_node(
    parent_tree: list,
    *,
    sheet_name: str,
    sheet_filename: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    project_name: str,
) -> list:
    """Append a `(sheet ...)` placeholder to the parent (root) schematic.

    The placeholder references `sheet_filename` (relative path) and is sized
    `width_mm × height_mm`. Returns the new node.

    The bottom-left corner of the sheet sits at MCP (`x_mm`, `y_mm`).
    """
    if find_sheet_by_filename(parent_tree, sheet_filename) is not None:
        raise ValueError(f"sheet {sheet_filename!r} already registered")
    if find_sheet_by_name(parent_tree, sheet_name) is not None:
        raise ValueError(f"sheet name {sheet_name!r} already in use")

    page_h = page_height_mm(parent_tree)
    # KiCAD's sheet block uses the TOP-LEFT corner as its (at ...) origin.
    # Convert MCP bottom-left + size → KiCAD top-left.
    bl_k = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    top_left_kicad = (bl_k[0], bl_k[1] - height_mm)
    root_uuid = _schematic_uuid(parent_tree)
    sheet_uuid = str(uuid.uuid4())

    def _prop(name: str, value: str, dy: float, hide: bool) -> list:
        node: list = [
            sym("property"),
            name,
            value,
            [sym("at"), round_mm(top_left_kicad[0]), round_mm(top_left_kicad[1] + dy), 0],
        ]
        effects: list = [sym("effects"), [sym("font"), [sym("size"), 1.27, 1.27]],
                         [sym("justify"), sym("left"), sym("bottom")]]
        if hide:
            effects.append([sym("hide"), sym("yes")])
        node.append(effects)
        return node

    sheet_node = [
        sym("sheet"),
        [sym("at"), round_mm(top_left_kicad[0]), round_mm(top_left_kicad[1])],
        [sym("size"), round_mm(width_mm), round_mm(height_mm)],
        [sym("fields_autoplaced"), sym("yes")],
        [sym("stroke"), [sym("width"), 0.1524], [sym("type"), sym("solid")]],
        [sym("fill"), [sym("color"), 0, 0, 0, 0.0]],
        [sym("uuid"), sheet_uuid],
        _prop("Sheetname", sheet_name, -2.54, hide=False),
        _prop("Sheetfile", sheet_filename, height_mm + 1.27, hide=False),
        [
            sym("instances"),
            [
                sym("project"),
                project_name,
                [sym("path"), f"/{root_uuid}", [sym("page"), "2"]],
            ],
        ],
    ]
    parent_tree.append(sheet_node)
    return sheet_node


def find_sheet_by_filename(tree: list, filename: str) -> list | None:
    """Find a `(sheet ...)` node by its Sheetfile property."""
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        for prop in find_children(node, "property"):
            if (
                len(prop) >= 3
                and prop[1] == "Sheetfile"
                and prop[2] == filename
            ):
                return node
    return None


def find_sheet_by_name(tree: list, sheet_name: str) -> list | None:
    """Find a `(sheet ...)` node by its Sheetname property."""
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        for prop in find_children(node, "property"):
            if (
                len(prop) >= 3
                and prop[1] == "Sheetname"
                and prop[2] == sheet_name
            ):
                return node
    return None


def get_sheet_uuid(sheet_node: list) -> str:
    u = find_child(sheet_node, "uuid")
    if not u or len(u) < 2 or not isinstance(u[1], str):
        raise ValueError("sheet has no uuid")
    return u[1]


def get_sheet_filename(sheet_node: list) -> str | None:
    for prop in find_children(sheet_node, "property"):
        if len(prop) >= 3 and prop[1] == "Sheetfile" and isinstance(prop[2], str):
            return prop[2]
    return None


def list_sheets(tree: list) -> list[dict]:
    """Return summaries of every (sheet ...) node in `tree`."""
    out = []
    for node in tree[1:]:
        if not is_call(node, "sheet"):
            continue
        name = ""
        filename = ""
        for prop in find_children(node, "property"):
            if len(prop) >= 3:
                if prop[1] == "Sheetname":
                    name = prop[2]
                elif prop[1] == "Sheetfile":
                    filename = prop[2]
        u = find_child(node, "uuid")
        sheet_uuid = u[1] if u and len(u) >= 2 else ""
        out.append({"name": name, "filename": filename, "uuid": sheet_uuid})
    return out


def add_hierarchical_label(
    tree: list,
    *,
    net_name: str,
    x_mm: float,
    y_mm: float,
    shape: str = "input",
    orientation: str = "right",
) -> list:
    """Append a `(hierarchical_label ...)` to the active sheet's tree."""
    if shape not in _VALID_LABEL_SHAPES:
        raise ValueError(f"shape must be one of {sorted(_VALID_LABEL_SHAPES)}")
    if orientation not in _LABEL_ORIENTATIONS:
        raise ValueError(
            f"orientation must be one of {list(_LABEL_ORIENTATIONS)}"
        )
    page_h = page_height_mm(tree)
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    require_on_grid(xk, yk, f"hierarchical label {net_name}", page_h)
    node = [
        sym("hierarchical_label"),
        net_name,
        [sym("shape"), sym(shape)],
        [sym("at"), round_mm(xk), round_mm(yk), _LABEL_ORIENTATIONS[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("left")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    tree.append(node)
    return node


def add_sheet_pin(
    sheet_node: list,
    *,
    pin_name: str,
    shape: str,
    x_mm: float,
    y_mm: float,
    page_h: float,
    orientation: str = "right",
) -> list:
    """Append a `(pin ...)` to a parent's `(sheet ...)` block.

    The pin's name must match a `(hierarchical_label ...)` of the same name
    inside the child schematic — that's how KiCAD wires the parent net into
    the child.
    """
    if shape not in _VALID_LABEL_SHAPES:
        raise ValueError(f"shape must be one of {sorted(_VALID_LABEL_SHAPES)}")
    if orientation not in _LABEL_ORIENTATIONS:
        raise ValueError(
            f"orientation must be one of {list(_LABEL_ORIENTATIONS)}"
        )
    xk, yk = mcp_to_kicad_xy(x_mm, y_mm, page_h)
    require_on_grid(xk, yk, f"sheet pin {pin_name}", page_h)
    pin_node = [
        sym("pin"),
        pin_name,
        sym(shape),
        [sym("at"), round_mm(xk), round_mm(yk), _LABEL_ORIENTATIONS[orientation]],
        [
            sym("effects"),
            [sym("font"), [sym("size"), 1.27, 1.27]],
            [sym("justify"), sym("right")],
        ],
        [sym("uuid"), str(uuid.uuid4())],
    ]
    sheet_node.append(pin_node)
    return pin_node


# --------------------------------------------------------------------------- #
# Pin position math
# --------------------------------------------------------------------------- #


def find_lib_symbol_def(tree: list, qualified_lib_id: str) -> list | None:
    """Look up the lib_symbols entry for a qualified id within the schematic."""
    block = find_child(tree, "lib_symbols")
    if not block:
        return None
    for child in block[1:]:
        if is_call(child, "symbol") and len(child) >= 2 and child[1] == qualified_lib_id:
            return child
    return None


def _iter_pins(symbol_def_node: list):
    """Yield (pin_node, parent_unit_node_or_None) for every pin in a lib symbol def."""
    for child in symbol_def_node[2:]:
        h = head_of(child)
        if h == "pin":
            yield child, None
        elif h == "symbol":
            for sub in child[2:]:
                if head_of(sub) == "pin":
                    yield sub, child


def _pin_local_at(pin_node: list) -> tuple[float, float, float]:
    """Return (x, y, angle) from `(pin ... (at x y angle) ...)` in lib coords (Y down)."""
    at = find_child(pin_node, "at")
    if not at or len(at) < 4:
        return 0.0, 0.0, 0.0
    return float(at[1]), float(at[2]), float(at[3])


def _pin_id(pin_node: list) -> tuple[str, str]:
    """Return (number, name) for a pin node."""
    number = ""
    name = ""
    for sub in pin_node[1:]:
        if is_call(sub, "number") and len(sub) >= 2 and isinstance(sub[1], str):
            number = sub[1]
        elif is_call(sub, "name") and len(sub) >= 2 and isinstance(sub[1], str):
            name = sub[1]
    return number, name


def list_pins_for_symbol(tree: list, reference: str) -> list[dict]:
    """List pins of a placed symbol with their absolute positions in MCP coords."""
    s_node = find_symbol_by_reference(tree, reference)
    if s_node is None:
        raise KeyError(f"no symbol with reference {reference!r}")
    lib_id_node = find_child(s_node, "lib_id")
    if not lib_id_node or len(lib_id_node) < 2:
        raise ValueError(f"symbol {reference!r} has no lib_id")
    qualified = lib_id_node[1]
    sym_def = find_lib_symbol_def(tree, qualified)
    if sym_def is None:
        raise KeyError(
            f"lib_symbols entry {qualified!r} missing — was the schematic written by us?"
        )

    at = find_child(s_node, "at")
    if not at or len(at) < 4:
        raise ValueError(f"symbol {reference!r} has malformed (at ...)")
    sx, sy, srot = float(at[1]), float(at[2]), float(at[3])
    page_h = page_height_mm(tree)

    out = []
    for pin_node, _unit in _iter_pins(sym_def):
        lx, ly, lrot = _pin_local_at(pin_node)
        # KiCAD rotation is CCW. Library coords have Y down; instance rotation
        # is also applied in those coords.
        rx, ry = rotate_xy(lx, ly, srot)
        # In KiCAD, pin local Y has the same orientation as schematic Y (both
        # "down"), but rotate_xy uses math convention (Y up). Since both
        # systems are consistent, rotate_xy + add gives the right result.
        wx_kicad = sx + rx
        wy_kicad = sy - ry  # flip because KiCAD Y is down vs math Y up
        mcp_x, mcp_y = wx_kicad, page_h - wy_kicad
        number, name = _pin_id(pin_node)
        out.append(
            {
                "number": number,
                "name": name,
                "position_mm": [round_mm(mcp_x), round_mm(mcp_y)],
                "angle": (lrot + srot) % 360,
            }
        )
    return out


def get_pin_position(tree: list, reference: str, pin_number: str) -> tuple[float, float]:
    """Return (x, y) in MCP coords for a specific pin of a placed symbol."""
    pins = list_pins_for_symbol(tree, reference)
    for p in pins:
        if p["number"] == pin_number:
            return tuple(p["position_mm"])  # type: ignore[return-value]
    raise KeyError(
        f"pin {pin_number!r} not found on {reference!r}; available: {[p['number'] for p in pins]}"
    )


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _schematic_uuid(tree: list) -> str:
    u = find_child(tree, "uuid")
    if u and len(u) >= 2 and isinstance(u[1], str):
        return u[1]
    new = str(uuid.uuid4())
    tree.insert(1, [sym("uuid"), new])
    return new
