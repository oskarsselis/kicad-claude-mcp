"""Schematic layout rules: grid-aligned coordinates, drawing-frame bounds,
Reference/Value placement, hidden power references, automatic junctions.

Pure tree manipulation, no KiCAD install needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_project

# Body 2.032 x 5.08 mm, pins at top and bottom ending at +/-3.81 (like Device:R).
_RESISTOR = """
(symbol "R"
  (property "Reference" "R" (at 2.032 0 90))
  (property "Value" "R" (at 0 0 90))
  (symbol "R_0_1"
    (rectangle (start -1.016 -2.54) (end 1.016 2.54)))
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27)))))))
"""

# Pins on the left and right, body 10.16 x 5.08 mm.
_TWO_SIDED = """
(symbol "U"
  (property "Reference" "U" (at 0 0 0))
  (property "Value" "U" (at 0 0 0))
  (symbol "U_0_1"
    (rectangle (start -5.08 -2.54) (end 5.08 2.54)))
  (symbol "U_1_1"
    (pin input line (at -7.62 0 0) (length 2.54)
      (name "IN" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))
    (pin output line (at 7.62 0 180) (length 2.54)
      (name "OUT" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27)))))))
"""

_POWER = """
(symbol "+5V"
  (power)
  (property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (property "Value" "+5V" (at 0 3.556 0) (effects (font (size 1.27 1.27))))
  (symbol "+5V_0_1"
    (polyline (pts (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27))))
  (symbol "+5V_1_1"
    (pin power_in line (at 0 0 90) (length 0)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))))
"""


def _def(text: str) -> list:
    return sexpdata.loads(text)


@pytest.fixture
def tree(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    yield sch_io.parse_file(files["sch"])
    state.clear_active()


def _place(tree, text, ref, lib_id, x, y, rot=0, value="10k"):
    return ed.add_symbol(
        tree, qualified_lib_id=lib_id, reference=ref, value=value,
        x_mm=x, y_mm=y, rotation=rot, sym_def_node=_def(text), project_name="p",
    )


def _prop(node, name):
    return next(p for p in sch_io.find_children(node, "property") if p[1] == name)


def _at(prop):
    at = sch_io.find_child(prop, "at")
    return float(at[1]), float(at[2]), float(at[3])


def _hidden(prop):
    effects = sch_io.find_child(prop, "effects")
    return any(sch_io.is_call(c, "hide") for c in effects[1:])


def _justify(prop):
    j = sch_io.find_child(sch_io.find_child(prop, "effects"), "justify")
    return str(j[1]) if j else None


# ===== Coordinates ========================================================= #


def test_flip_reference_is_grid_aligned(tree):
    assert ed.page_height_mm(tree) == pytest.approx(208.28)  # A4: 82 x 2.54
    assert (ed.page_height_mm(tree) / ed.SCH_GRID_MM) == pytest.approx(82)


def test_on_grid_mcp_point_lands_on_grid_in_file(tree):
    wire = ed.add_wire(tree, 101.6, 101.6, 127.0, 101.6)
    for xy in sch_io.find_children(sch_io.find_child(wire, "pts"), "xy"):
        for v in xy[1:]:
            assert (v / 2.54) == pytest.approx(round(v / 2.54), abs=1e-6)


def test_symbol_outside_drawing_frame_is_refused(tree):
    with pytest.raises(ValueError, match="outside the drawing frame"):
        _place(tree, _RESISTOR, "R1", "L:R", 100, -80)
    assert ed.find_symbol_by_reference(tree, "R1") is None
    assert ed.find_lib_symbol_def(tree, "L:R") is None  # nothing injected


def test_wire_and_label_outside_frame_are_refused(tree):
    with pytest.raises(ValueError, match="outside the drawing frame"):
        ed.add_wire(tree, 100, 100, 100, 300)
    with pytest.raises(ValueError, match="outside the drawing frame"):
        ed.add_label(tree, "X", 5, 100)


# ===== Reference / Value placement ========================================= #


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_ref_and_value_sit_outside_symbol_on_two_lines(tree, rot):
    # Pins are 150 mil from the centre along the body: offset that axis half a step.
    x, y = (101.6, 100.33) if rot in (0, 180) else (100.33, 101.6)
    s = _place(tree, _RESISTOR, "R1", "L:R", x, y, rot)
    at = sch_io.find_child(s, "at")
    bbox, _ = ed.symbol_outline(_def(_RESISTOR), float(at[1]), float(at[2]), rot)
    x0, y0, x1, y1 = bbox
    rx, ry, rangle = _at(_prop(s, "Reference"))
    vx, vy, vangle = _at(_prop(s, "Value"))
    # Stacked: same column, one line apart, Reference on top.
    assert rx == vx
    assert vy - ry == pytest.approx(ed.FIELD_LINE_MM)
    # Anchor is clear of the symbol outline (text grows away from it).
    assert rx >= x1 + ed.FIELD_GAP_MM - 1e-6 or rx <= x0 - ed.FIELD_GAP_MM + 1e-6 \
        or ry >= y1 or vy <= y0
    # Stored angle makes the text read horizontally at every rotation.
    assert rangle == vangle == (90 if rot in (90, 270) else 0)
    assert not _hidden(_prop(s, "Reference"))
    assert not _hidden(_prop(s, "Value"))


def test_vertical_resistor_text_goes_right_left_justified(tree):
    s = _place(tree, _RESISTOR, "R1", "L:R", 101.6, 100.33)
    x, y = float(sch_io.find_child(s, "at")[1]), float(sch_io.find_child(s, "at")[2])
    rx, ry, _ = _at(_prop(s, "Reference"))
    assert rx == pytest.approx(x + 1.016 + ed.FIELD_GAP_MM)
    assert ry == pytest.approx(y - 1.27)
    assert _justify(_prop(s, "Reference")) == "left"


def test_pins_left_and_right_push_text_above(tree):
    s = _place(tree, _TWO_SIDED, "U1", "L:U", 101.6, 101.6, value="BUF")
    y = float(sch_io.find_child(s, "at")[2])
    _, ry, _ = _at(_prop(s, "Reference"))
    _, vy, _ = _at(_prop(s, "Value"))
    assert ry < vy < y - 2.54  # both above the body top edge


def test_move_symbol_carries_fields(tree):
    _place(tree, _RESISTOR, "R1", "L:R", 101.6, 100.33)
    ed.move_symbol(tree, "R1", 152.4, 125.73)
    s = ed.find_symbol_by_reference(tree, "R1")
    x, y = float(sch_io.find_child(s, "at")[1]), float(sch_io.find_child(s, "at")[2])
    rx, ry, _ = _at(_prop(s, "Reference"))
    assert rx == pytest.approx(x + 1.016 + ed.FIELD_GAP_MM)
    assert ry == pytest.approx(y - 1.27)
    fx, fy, _ = _at(_prop(s, "Footprint"))
    assert (fx, fy) == (x, y)


# ===== Power symbols ======================================================= #


def test_power_symbol_reference_hidden_value_from_library(tree):
    s = _place(tree, _POWER, "#PWR0001", "power:+5V", 101.6, 152.4, value="+5V")
    x, y = float(sch_io.find_child(s, "at")[1]), float(sch_io.find_child(s, "at")[2])
    assert _hidden(_prop(s, "Reference"))
    assert not _hidden(_prop(s, "Value"))
    vx, vy, _ = _at(_prop(s, "Value"))
    assert (vx, vy) == pytest.approx((x, y - 3.556))  # above the symbol


# ===== Junctions =========================================================== #


def _junctions(tree):
    return [
        (float(j[1][1]), float(j[1][2]))
        for j in tree[1:] if sch_io.is_call(j, "junction")
    ]


def test_t_onto_wire_middle_adds_junction(tree):
    ed.add_wire(tree, 101.6, 127.0, 101.6, 101.6)
    ed.add_wire(tree, 101.6, 114.3, 114.3, 114.3)
    page_h = ed.page_height_mm(tree)
    assert _junctions(tree) == [pytest.approx((101.6, page_h - 114.3))]


def test_wire_across_existing_wire_end_adds_junction(tree):
    ed.add_wire(tree, 101.6, 114.3, 114.3, 114.3)
    ed.add_wire(tree, 101.6, 127.0, 101.6, 101.6)  # passes through the first wire's end
    assert len(_junctions(tree)) == 1


def test_corner_of_two_wires_has_no_junction(tree):
    ed.add_wire(tree, 101.6, 127.0, 101.6, 101.6)
    ed.add_wire(tree, 101.6, 101.6, 127.0, 101.6)
    assert _junctions(tree) == []


def test_three_wire_ends_meeting_add_one_junction(tree):
    ed.add_wire(tree, 101.6, 127.0, 101.6, 114.3)
    ed.add_wire(tree, 101.6, 114.3, 101.6, 101.6)
    ed.add_wire(tree, 101.6, 114.3, 127.0, 114.3)
    ed.add_wire(tree, 88.9, 114.3, 101.6, 114.3)  # fourth end: still one junction
    assert len(_junctions(tree)) == 1


# ===== 100 mil grid enforcement ============================================ #


def test_off_grid_wire_is_refused_with_nearest_point(tree):
    with pytest.raises(ValueError, match=r"off the 100 mil.*nearest on-grid point is \(101\.6, 101\.6\)"):
        ed.add_wire(tree, 101.0, 101.6, 127.0, 101.6)
    assert not sch_io.find_children(tree, "wire")


def test_off_grid_label_is_refused(tree):
    with pytest.raises(ValueError, match="off the 100 mil"):
        ed.add_label(tree, "VOUT", 111.0, 121.92)


def test_symbol_with_pins_off_grid_is_refused_with_suggestion(tree):
    # Centre on grid puts Device:R-style pins (+/-150 mil) off grid.
    with pytest.raises(ValueError, match=r"place it at MCP \(101\.6, 102\.87\)"):
        _place(tree, _RESISTOR, "R1", "L:R", 101.6, 101.6)
    assert ed.find_lib_symbol_def(tree, "L:R") is None  # nothing injected
    _place(tree, _RESISTOR, "R1", "L:R", 101.6, 102.87)  # the suggestion works


def test_move_symbol_off_grid_is_refused(tree):
    _place(tree, _RESISTOR, "R1", "L:R", 101.6, 100.33)
    with pytest.raises(ValueError, match="off the 100 mil"):
        ed.move_symbol(tree, "R1", 101.6, 101.6)
    s = ed.find_symbol_by_reference(tree, "R1")
    assert float(sch_io.find_child(s, "at")[2]) == pytest.approx(ed.page_height_mm(tree) - 100.33)


# ===== Derived symbols (extends) =========================================== #

_MINILIB = Path(__file__).parent / "fixtures" / "MiniLib.kicad_sym"


def test_fetch_symbol_def_flattens_extends():
    node = ed.fetch_symbol_def(_MINILIB, "R_Small")
    assert sch_io.find_child(node, "extends") is None
    assert ed.get_symbol_property(node, "Value") == "R_Small"  # derived wins
    assert ed.get_symbol_property(node, "Footprint") == "Resistor_SMD:R_0603_1608Metric"
    units = [c[1] for c in node[2:] if sch_io.head_of(c) == "symbol"]
    assert units == ["R_Small_0_1"]
    assert sorted(ed.collect_pin_numbers(node)) == ["1", "2"]


def test_get_symbol_pins_follows_extends():
    from kicad_claude.indexer.kicad_libs import get_symbol_pins

    assert [p["number"] for p in get_symbol_pins(_MINILIB, "R_Small")] == ["1", "2"]


def _size(prop):
    font = ed.find_child(ed.find_child(prop, "effects"), "font")
    return float(ed.find_child(font, "size")[1])


def test_text_size_applies_to_visible_fields_only(tree):
    s = ed.add_symbol(
        tree, qualified_lib_id="L:R", reference="R1", value="10k", x_mm=101.6, y_mm=100.33,
        rotation=0, sym_def_node=_def(_RESISTOR), project_name="t", text_size=1.016,
    )
    assert _size(_prop(s, "Reference")) == _size(_prop(s, "Value")) == 1.016
    assert _size(_prop(s, "Footprint")) == 1.27
    p = ed.add_symbol(
        tree, qualified_lib_id="power:+5V", reference="#PWR0001", value="+5V", x_mm=101.6,
        y_mm=152.4, rotation=0, sym_def_node=_def(_POWER), project_name="t", text_size=1.016,
    )
    assert _size(_prop(p, "Value")) == 1.016


def test_tools_use_the_project_text_size(tmp_path):
    from mcp.server.fastmcp import FastMCP

    from kicad_claude.adapters import project_settings as ps
    from kicad_claude.tools import schematic as sch_tools

    state.clear_active()
    files = write_blank_project(tmp_path / "p", "p")
    state.set_active(tmp_path / "p", "p")
    try:
        mcp = FastMCP("t")
        sch_tools.register(mcp)
        mcp._tool_manager.get_tool("set_schematic_text_size").fn(size_mils=40)
        assert ps.get_schematic_text_size_mils(ps.load_pro(files["pro"])) == 40
        assert sch_tools._field_text_mm() == pytest.approx(1.016)
    finally:
        state.clear_active()


# ===== Drawing style: secondary fields, blocks, power wires, checks ======== #


def test_shown_fields_are_secondary_smaller_and_grey(tree):
    s = ed.add_symbol(
        tree, qualified_lib_id="L:R", reference="C1", value="10µF", x_mm=101.6, y_mm=100.33,
        rotation=0, sym_def_node=_def(_RESISTOR), project_name="t", text_size=1.016,
        fields={"Voltage": "50 V", "Dielectric": "X7R"}, show_fields=["Voltage"],
    )
    volt = _prop(s, "Voltage")
    font = sch_io.find_child(sch_io.find_child(volt, "effects"), "font")
    assert _size(volt) == pytest.approx(1.016 * ed.SECONDARY_SCALE, abs=1e-3)
    color = sch_io.find_child(font, "color")
    assert [int(c) for c in color[1:4]] == list(ed.SECONDARY_COLOR[:3])
    assert _hidden(_prop(s, "Dielectric"))
    # Voltage sits below the Value, in the same column
    assert _at(volt)[0] == _at(_prop(s, "Value"))[0] and _at(volt)[1] > _at(_prop(s, "Value"))[1]


def test_add_block_draws_dashed_rectangle_with_title_and_refuses_overlap(tree):
    ed.add_block(tree, "Power", 50.8, 101.6, 76.2, 50.8)
    rect = next(c for c in tree if sch_io.is_call(c, "rectangle"))
    stroke = sch_io.find_child(rect, "stroke")
    assert str(sch_io.find_child(stroke, "type")[1]) == "dash"
    blocks = list(ed.iter_blocks(tree))
    assert blocks[0][0] == "Power"
    with pytest.raises(ValueError, match="overlaps"):
        ed.add_block(tree, "Other", 101.6, 127.0, 50.8, 50.8)
    ed.add_block(tree, "Other", 132.08, 101.6, 25.4, 25.4)        # beside it: fine
    ed.add_block(tree, "Off grid", 15.0, 101.6, 25.4, 25.4)        # corners may be off grid
    with pytest.raises(ValueError):
        ed.add_block(tree, "Outside", 5.0, 50.8, 5.0, 25.4)         # left of the frame


def test_block_spacing_wants_one_gap_to_blocks_frame_and_title_block(tree):
    from kicad_claude.adapters import sch_verify

    # A4: frame x 10..287, y 8.28..198.28; title block x 177..287, y 8.28..42.28 (MCP)
    ed.add_block(tree, "A", 15.0, 13.28, 157.0, 180.0)
    ed.add_block(tree, "B", 177.0, 47.28, 105.0, 146.0)
    gaps = sch_verify.block_gaps(tree)
    assert {v for g in gaps for k, v in g.items() if k != "block"} == {5.0}
    assert not sch_verify.style_issues(tree)["block_spacing"]
    rect = [c for c in tree if sch_io.is_call(c, "rectangle")][1]
    sch_io.find_child(rect, "start")[2] = 13.0                      # B's top: 3 mm from the frame
    title = next(c for c in tree if sch_io.is_call(c, "text") and c[1] == "B")
    sch_io.find_child(title, "at")[2] = 13.0 + ed.BLOCK_TITLE_INSET_MM
    issues = sch_verify.style_issues(tree)["block_spacing"]
    assert [e["block"] for e in issues] == ["B"] and issues[0]["top"] == 3.0


def test_color_power_wires_colours_only_wires_on_power_nets(tree):
    _place(tree, _POWER, "#PWR0001", "power:+5V", 101.6, 152.4, value="+5V")
    ed.add_wire(tree, 101.6, 152.4, 114.3, 152.4)                  # on +5V
    ed.add_wire(tree, 114.3, 152.4, 114.3, 139.7)                  # joined to it
    ed.add_wire(tree, 127.0, 152.4, 139.7, 152.4)                  # unrelated
    res = ed.color_power_wires(tree)
    assert res["nets"] == ["+5V"] and res["colored_wires"] == 2
    wires = [c for c in tree if sch_io.is_call(c, "wire")]
    assert [ed.wire_color(w) for w in wires] == [(255, 0, 0), (255, 0, 0), None]


def test_style_issues_flags_long_wires_lone_test_points_and_long_values(tree):
    from kicad_claude.adapters import sch_verify

    ed.add_wire(tree, 25.4, 101.6, 254.0, 101.6)                   # A4: limit 74.25 mm
    _place(tree, _POWER, "#PWR0001", "power:+5V", 101.6, 152.4, value="+5V")
    ed.add_wire(tree, 101.6, 152.4, 114.3, 152.4)                  # power wire, not red
    tp_def = """
(symbol "TestPoint"
  (property "Reference" "TP" (at 0 3 0))
  (property "Value" "TP" (at 0 5 0))
  (symbol "TestPoint_0_1" (circle (center 0 3.302) (radius 0.762)))
  (symbol "TestPoint_1_1"
    (pin passive line (at 0 0 90) (length 2.54)
      (name "1" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))))
"""
    _place(tree, tp_def, "TP1", "Connector:TestPoint", 76.2, 152.4, value="VBUS")
    _place(tree, _RESISTOR, "R1", "L:R", 50.8, 151.13, value="5 mΩ 1 %, ≥ 0.25 W")
    issues = sch_verify.style_issues(tree)
    assert issues["long_wires"] and issues["long_wires"][0]["span_mm"] == pytest.approx(228.6)
    assert [e["reference"] for e in issues["lone_test_points"]] == ["TP1"]
    assert [e["reference"] for e in issues["long_values"]] == ["R1"]
    assert issues["uncolored_power_wires"][0]["nets"] == ["+5V"]
    assert not issues["ref_value_layout"]
    ed.color_power_wires(tree)
    assert not sch_verify.style_issues(tree)["uncolored_power_wires"]


def test_style_issues_flags_wires_and_symbols_across_a_block_border(tree):
    from kicad_claude.adapters import sch_verify

    ed.add_block(tree, "A", 50.8, 101.6, 50.8, 50.8)
    ed.add_wire(tree, 76.2, 127.0, 127.0, 127.0)                    # leaves the block
    _place(tree, _RESISTOR, "R1", "L:R", 101.6, 115.57)             # on the right border
    issues = sch_verify.style_issues(tree)
    items = {e["item"] for e in issues["crossing_blocks"]}
    assert items == {"wire", "R1"}
