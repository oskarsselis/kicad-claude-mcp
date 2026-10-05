"""Phase 7 — validation tools (ERC / DRC).

Tools:
    run_erc           — Electrical Rules Check on the active schematic
    run_drc           — Design Rules Check on the active PCB
    verify_schematic  — every schematic check in one call (ERC, nets, grid,
                        frame, crowding) plus a PDF to look at

Both shell out to `kicad-cli` and return structured JSON (errors,
warnings, violations with positions). Raw report JSON is also written next
to the source file so it can be inspected by humans.
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import kicad_cli, sch_verify

logger = logging.getLogger("kicad-claude.tools.validation")


def register(mcp) -> None:
    """Register Phase 7 tools on the FastMCP instance."""

    @mcp.tool()
    def verify_schematic(timeout_seconds: float = 120.0) -> dict:
        """Check the active project's schematic (root and all sub-sheets) before
        reporting it as done. Run this after every schematic change.

        Checks: KiCAD ERC (all severities), off-grid connection points (100 mil),
        items outside the drawing frame or on the title block, and symbols
        crowding each other (overlapping bounding boxes, text included).
        Also returns the netlist (net -> REF.pin) to compare with the intended
        circuit, nets with a single connection, and a PDF of the schematic.

        `ok` is true only when every check passes; `problems` lists what failed.
        Outputs are written to `<project>/verify/`.
        """
        proj = state.get_active()
        return sch_verify.verify(proj.path, proj.name, timeout=timeout_seconds)

    @mcp.tool()
    def run_erc(severity: str = "all", timeout_seconds: float = 60.0) -> dict:
        """Run KiCAD's Electrical Rules Check on the active schematic.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'. Maps to
                `kicad-cli sch erc --severity-<value>`.
            timeout_seconds: hard cap on the kicad-cli invocation.

        Returns counts by severity, the list of violations with positions,
        and the path to the raw JSON report.
        """
        proj = state.get_active()
        return kicad_cli.run_erc(
            proj.sch_path, severity=severity, timeout=timeout_seconds
        )

    @mcp.tool()
    def run_drc(
        severity: str = "all",
        schematic_parity: bool = True,
        all_track_errors: bool = False,
        refill_zones: bool = False,
        timeout_seconds: float = 120.0,
    ) -> dict:
        """Run KiCAD's Design Rules Check on the active PCB.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'.
            schematic_parity: include parity check between PCB and schematic.
            all_track_errors: report each individual track error (more verbose).
            refill_zones: refill zones before validation (use after add_zone /
                add_ground_plane). Saves the board with refilled zones.
            timeout_seconds: hard cap.

        Returns counts, violations, unconnected items, parity findings, and
        the path to the raw JSON report.
        """
        return kicad_cli.run_drc(
            state.get_active_board_path(),
            severity=severity,
            schematic_parity=schematic_parity,
            all_track_errors=all_track_errors,
            refill_zones=refill_zones,
            timeout=timeout_seconds,
        )
