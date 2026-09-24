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
