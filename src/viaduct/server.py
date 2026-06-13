"""viaduct — MCP server for PCB work in KiCad.

Reads/edits .kicad_pcb and .kicad_sch files with a built-in s-expression
parser, and shells out to kicad-cli for checks, exports, and renders.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP, Image
from pydantic import BaseModel, Field

from . import cli_ops, kicad_cli, safety
from .board import Board
from .schematic import Schematic

mcp = FastMCP(
    "viaduct",
    instructions=(
        "PCB tools for KiCad. Editing tools write a .bak backup next to the file "
        "and refuse to edit files currently open in KiCad (lockfile check). "
        "All coordinates are millimetres in the board frame (X right, Y down); "
        "rotations are degrees counter-clockwise. Typical placement loop: "
        "board_info + design_rules + list_footprints + connectivity + ratsnest "
        "to understand the design, move_footprints to place, render_board_png "
        "to inspect, ratsnest again to measure, run_drc to verify."
    ),
)


class Move(BaseModel):
    reference: str = Field(description="Footprint reference designator, e.g. 'C3'")
    x_mm: float = Field(description="New X position in mm")
    y_mm: float = Field(description="New Y position in mm (Y axis points down)")
    rotation_deg: float | None = Field(
        default=None,
        description="New absolute rotation in degrees CCW; omit to keep current rotation",
    )


# ---------------------------------------------------------------------------
# sanity / checks
# ---------------------------------------------------------------------------

@mcp.tool()
def kicad_version() -> dict:
    """Check that KiCad is installed and report kicad-cli path and version."""
    cli = kicad_cli.find_kicad_cli()
    return {"kicad_cli": cli, "version": kicad_cli.version()}


@mcp.tool()
def run_drc(board_path: str, severity: str = "all", schematic_parity: bool = False,
            refill_zones: bool = False) -> dict:
    """Run KiCad's Design Rules Check on a .kicad_pcb file.

    Returns a summary with counts by severity/type and each violation's
    description and position in mm. severity: 'all', 'error', or 'warning'.
    """
    return cli_ops.run_drc(board_path, severity, schematic_parity, refill_zones)


@mcp.tool()
def run_erc(schematic_path: str, severity: str = "all") -> dict:
    """Run KiCad's Electrical Rules Check on a .kicad_sch file.

    Returns a summary with counts by severity/type and each violation's
    description and position in mm. severity: 'all', 'error', or 'warning'.
    """
    return cli_ops.run_erc(schematic_path, severity)


# ---------------------------------------------------------------------------
# board inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def board_info(board_path: str) -> dict:
    """Board overview: layers, thickness, outline bounding box, element counts."""
    return Board(board_path).board_info()


@mcp.tool()
def list_footprints(board_path: str) -> list[dict]:
    """All footprints with reference, value, position (mm), rotation, layer."""
    return Board(board_path).list_footprints()


@mcp.tool()
def list_nets(board_path: str) -> list[str]:
    """All net names on the board."""
    return Board(board_path).list_nets()


@mcp.tool()
def connectivity(board_path: str, references: list[str] | None = None) -> list[dict]:
    """Per footprint, each pad's net name and absolute position in mm.

    Pass references (e.g. ["U1", "C3"]) to limit output to those footprints.
    """
    return Board(board_path).connectivity(references)


@mcp.tool()
def courtyards(board_path: str) -> list[dict]:
    """Per footprint, the courtyard bounding box in absolute board mm.

    Use this to check spacing: two footprints whose courtyard boxes overlap
    will fail DRC.
    """
    return Board(board_path).courtyards()


@mcp.tool()
def ratsnest(board_path: str) -> dict:
    """Unrouted net connections as airwires (from-pad, to-pad, length in mm).

    The primary placement-quality metric: total_length_mm should go down as
    placement improves. Pads already joined by tracks/vias are excluded;
    zone fills are ignored.
    """
    return Board(board_path).ratsnest()


@mcp.tool()
def design_rules(board_path: str) -> dict:
    """Clearance / track-width / via design rules.

    Reads board-file setup plus the sibling .kicad_pro project file
    (minimum rules and per-netclass clearance/track width) when present.
    """
    return Board(board_path).design_rules()


# ---------------------------------------------------------------------------
# board editing
# ---------------------------------------------------------------------------

@mcp.tool()
def move_footprint(board_path: str, reference: str, x_mm: float, y_mm: float,
                   rotation_deg: float | None = None) -> dict:
    """Move (and optionally rotate) one footprint, then save the board.

    Writes a .bak backup first and refuses if the board is open in KiCad.
    Rotation is absolute degrees CCW; pad angles are adjusted automatically.
    """
    board = Board(board_path)
    result = board.move_footprint(reference, x_mm, y_mm, rotation_deg)
    result["backup"] = board.save()
    return result


@mcp.tool()
def move_footprints(board_path: str, moves: list[Move]) -> dict:
    """Move several footprints in one batch, then save the board once.

    Preferred over repeated move_footprint calls: one backup, one write.
    """
    board = Board(board_path)
    results = [
        board.move_footprint(m.reference, m.x_mm, m.y_mm, m.rotation_deg) for m in moves
    ]
    backup = board.save()
    return {"moved": results, "count": len(results), "backup": backup}


@mcp.tool()
def set_board_outline_rect(board_path: str, x_mm: float, y_mm: float,
                           width_mm: float, height_mm: float) -> dict:
    """Replace the board outline with a rectangle on Edge.Cuts, then save.

    (x_mm, y_mm) is the top-left corner. Removes all existing Edge.Cuts
    graphics first. Writes a .bak backup; refuses if open in KiCad.
    """
    board = Board(board_path)
    result = board.set_board_outline_rect(x_mm, y_mm, width_mm, height_mm)
    result["backup"] = board.save()
    return result


@mcp.tool()
def restore_backup(file_path: str) -> dict:
    """Restore a .kicad_pcb/.kicad_sch file from the .bak written before the last edit."""
    restored = safety.restore_backup(file_path)
    return {"restored": restored, "from": safety.backup_path(file_path)}


# ---------------------------------------------------------------------------
# schematic
# ---------------------------------------------------------------------------

@mcp.tool()
def list_symbols(schematic_path: str) -> list[dict]:
    """All placed schematic symbols with reference, value, lib_id, footprint, position."""
    return Schematic(schematic_path).list_symbols()


@mcp.tool()
def list_labels(schematic_path: str) -> list[dict]:
    """All net labels (local, global, hierarchical) with text and position."""
    return Schematic(schematic_path).list_labels()


@mcp.tool()
def set_symbol_property(schematic_path: str, reference: str, property_name: str,
                        value: str) -> dict:
    """Set a property (Value, Footprint, MPN, ...) on a schematic symbol, then save.

    Writes a .bak backup first and refuses if the schematic is open in KiCad.
    """
    sch = Schematic(schematic_path)
    result = sch.set_symbol_property(reference, property_name, value)
    result["backup"] = sch.save()
    return result


# ---------------------------------------------------------------------------
# exports / rendering
# ---------------------------------------------------------------------------

@mcp.tool()
def export_gerbers(board_path: str, output_dir: str | None = None,
                   include_drill: bool = True) -> dict:
    """Export fabrication Gerbers (and drill files) to a directory.

    Defaults to <board_dir>/viaduct_out/.
    """
    return cli_ops.export_gerbers(board_path, output_dir, include_drill)


@mcp.tool()
def export_bom(schematic_path: str, output_path: str | None = None,
               group_by: str = "Value,Footprint", exclude_dnp: bool = False) -> dict:
    """Export a CSV Bill of Materials from the schematic; returns the CSV content too."""
    return cli_ops.export_bom(schematic_path, output_path, group_by, exclude_dnp)


@mcp.tool()
def export_step(board_path: str, output_path: str | None = None) -> dict:
    """Export a 3D STEP model of the board (for mechanical CAD)."""
    return cli_ops.export_step(board_path, output_path)


@mcp.tool()
def export_netlist(schematic_path: str, output_path: str | None = None,
                   format: str = "kicadsexpr") -> dict:
    """Export a netlist from the schematic (kicadsexpr, kicadxml, spice, ...)."""
    return cli_ops.export_netlist(schematic_path, output_path, format)


@mcp.tool()
def render_board_svg(board_path: str, output_path: str | None = None,
                     layers: str = cli_ops.DEFAULT_SVG_LAYERS) -> dict:
    """Render selected board layers to a single SVG file (board area only).

    layers is a comma-separated KiCad layer list, e.g. 'F.Cu,F.SilkS,Edge.Cuts'.
    """
    return cli_ops.render_svg(board_path, output_path, layers)


@mcp.tool()
def render_board_png(board_path: str, output_path: str | None = None,
                     width: int = 1200, height: int = 900, side: str = "top",
                     zoom: float = 0.9) -> Image:
    """Render the board to a PNG image and return it for visual review.

    Use after moving footprints to inspect placement. side: top, bottom,
    left, right, front, back.
    """
    out = cli_ops.render_png(board_path, output_path, width, height, side, zoom)
    return Image(path=out)


def main() -> None:
    # stdio transport: this is what `claude mcp add viaduct -- viaduct` expects
    mcp.run()


if __name__ == "__main__":
    main()
