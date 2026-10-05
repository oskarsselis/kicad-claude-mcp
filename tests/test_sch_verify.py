"""verify_schematic: netlist parsing, geometry checks, crowding detection.

Unit tests run without KiCAD; the slow test builds real schematics and
runs the whole tool through kicad-cli.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import sexpdata

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io, sch_verify
from kicad_claude.indexer import kicad_libs
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli

_NETLIST = """
(export (version "E")
  (nets
    (net (code "1") (name "+5V") (class "Default")
      (node (ref "R1") (pin "1") (pintype "passive")))
    (net (code "2") (name "/VOUT") (class "Default")
      (node (ref "R1") (pin "2") (pintype "passive"))
      (node (ref "R2") (pin "1") (pintype "passive")))))
"""


def test_parse_netlist():
    assert sch_verify.parse_netlist(_NETLIST) == {"+5V": ["R1.1"], "/VOUT": ["R1.2", "R2.1"]}


@pytest.fixture
def tree(tmp_path: Path):
    files = write_blank_project(tmp_path / "p", "p")
    return sch_io.parse_file(files["sch"])


def test_geometry_flags_off_grid_and_outside_frame(tree):
    ed.add_wire(tree, 101.6, 101.6, 127.0, 101.6)  # fine
    # Written directly, as a hand-edited file could contain them:
    tree.append(sexpdata.loads('(wire (pts (xy 100 100) (xy 120 100)) (uuid "a"))'))
    tree.append(sexpdata.loads('(label "X" (at 101.6 203.2 0) (uuid "b"))'))
    issues = sch_verify.geometry_issues(tree, "p.kicad_sch")
    assert [e["item"] for e in issues["off_grid"]] == ["wire end", "wire end"]
    assert [e["item"] for e in issues["outside_frame"]] == ["label X"]
    assert issues["on_title_block"] == []


def test_geometry_flags_title_block(tree):
    tree.append(sexpdata.loads('(label "TB" (at 254 190.5 0) (uuid "c"))'))
    issues = sch_verify.geometry_issues(tree, "p.kicad_sch")
    assert [e["item"] for e in issues["on_title_block"]] == ["label TB"]


def test_drawn_references_skip_hidden_in_both_formats():
    tree = sexpdata.loads("""
    (kicad_sch
      (symbol (lib_id "Device:R") (property "Reference" "R1" (at 0 0 0)))
      (symbol (lib_id "power:GND") (property "Reference" "#PWR01" (at 0 0 0) (hide yes)))
      (symbol (lib_id "power:+5V")
        (property "Reference" "#PWR02" (at 0 0 0) (effects (font (size 1 1)) (hide yes)))))
    """)
    assert sch_verify.drawn_references([tree]) == Counter({"R1": 1})


def test_crowded_symbols_detects_double_plot():
    svg = "<desc>J1</desc><desc>5V IN</desc><desc>R1</desc><desc>J1</desc><desc>U1A</desc>"
    expected = Counter({"J1": 1, "R1": 1, "U1": 1})
    assert sch_verify.crowded_symbols([svg], expected) == ["J1"]
    # Two units of U1 drawn once each is not crowding.
    assert sch_verify.crowded_symbols(["<desc>U1A</desc><desc>U1B</desc>"], Counter({"U1": 2})) == []


# ===== Slow: the whole tool through kicad-cli ============================== #


def _project(tmp_path: Path, name: str):
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    cached = kicad_libs.load_cache()
    if cached is None:
        pytest.skip("library index not built")
    state.clear_active()
    lib_tools._index = cached
    write_blank_project(tmp_path / name, name)
    state.set_active(tmp_path / name, name)

    from mcp.server.fastmcp import FastMCP
    from kicad_claude.tools import schematic, validation

    mcp = FastMCP("t")
    for mod in (lib_tools, schematic, validation):
        mod.register(mcp)
    return lambda _tool, **kw: mcp._tool_manager.get_tool(_tool).fn(**kw)


def _divider(call, flag_x: float):
    """5V connector + PWR_FLAG + 10k/1k divider; flag_x=60.96 crowds J1."""
    call("add_symbol", lib_id="Connector:Conn_01x02_Pin", reference="J1", value="5V IN",
         x_mm=50.8, y_mm=139.7)
    call("add_power_symbol", net="PWR_FLAG", x_mm=flag_x, y_mm=139.7)
    call("add_power_symbol", net="PWR_FLAG", x_mm=53.34, y_mm=129.54)
    call("add_symbol", lib_id="Device:R", reference="R1", value="10k", x_mm=76.2, y_mm=135.89)
    call("add_symbol", lib_id="Device:R", reference="R2", value="1k", x_mm=76.2, y_mm=125.73)
    call("add_wire", x1_mm=55.88, y1_mm=139.7, x2_mm=flag_x, y2_mm=139.7)
    call("add_wire", x1_mm=flag_x, y1_mm=139.7, x2_mm=76.2, y2_mm=139.7)
    call("add_wire", x1_mm=76.2, y1_mm=132.08, x2_mm=76.2, y2_mm=129.54)
    call("add_wire", x1_mm=55.88, y1_mm=137.16, x2_mm=58.42, y2_mm=137.16)
    call("add_wire", x1_mm=58.42, y1_mm=137.16, x2_mm=58.42, y2_mm=129.54)
    call("add_wire", x1_mm=53.34, y1_mm=129.54, x2_mm=58.42, y2_mm=129.54)
    call("add_wire", x1_mm=58.42, y1_mm=129.54, x2_mm=58.42, y2_mm=119.38)
    call("add_wire", x1_mm=58.42, y1_mm=119.38, x2_mm=76.2, y2_mm=119.38)
    call("add_wire", x1_mm=76.2, y1_mm=121.92, x2_mm=76.2, y2_mm=119.38)
    call("add_power_symbol", net="GND", x_mm=58.42, y_mm=119.38)
    call("add_label", net_name="VOUT", x_mm=76.2, y_mm=129.54)


@pytest.mark.slow
def test_verify_passes_clean_schematic(tmp_path: Path):
    call = _project(tmp_path, "clean")
    _divider(call, flag_x=63.5)
    res = call("verify_schematic")
    state.clear_active()
    assert res["ok"], res["problems"]
    assert res["nets"]["/VOUT"] == ["R1.2", "R2.1"]
    assert sorted(res["nets"]["GND"]) == ["J1.2", "R2.2"]
    assert Path(res["pdf"]).is_file()


@pytest.mark.slow
def test_verify_reports_crowding(tmp_path: Path):
    call = _project(tmp_path, "crowded")
    _divider(call, flag_x=60.96)  # PWR_FLAG text runs into J1
    res = call("verify_schematic")
    state.clear_active()
    assert not res["ok"]
    assert res["crowded_symbols"] == ["J1"]
    assert any("crowded" in p for p in res["problems"])
