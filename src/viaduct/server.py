"""viaduct — MCP server for PCB work in KiCad.

Reads/edits .kicad_pcb and .kicad_sch files with a built-in s-expression
parser, and shells out to kicad-cli for checks, exports, and renders.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP, Image
from pydantic import BaseModel, Field

from . import cli_ops, footprints, kicad_cli, reports, safety
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
            refill_zones: bool = False, detail: str = "summary", top_n: int = 10) -> dict:
    """Run KiCad's Design Rules Check on a .kicad_pcb file.

    severity: 'all', 'error', or 'warning'. detail controls response size:
    'summary' (default) returns counts by severity/type plus the top_n worst
    items; 'full' returns every violation; or pass a violation type (e.g.
    'courtyards_overlap') to get just those. Each item has a description and
    position in mm.
    """
    full = cli_ops.run_drc(board_path, severity, schematic_parity, refill_zones)
    return reports.compact_drc(full, detail, top_n)


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
                   rotation_deg: float | None = None,
                   reject_on_collision: bool = False, clearance_mm: float = 0.0) -> dict:
    """Move (and optionally rotate) one footprint, then save the board.

    Writes a .bak backup first and refuses if the board is open in KiCad.
    Rotation is absolute degrees CCW; pad angles are adjusted automatically.
    The result always reports any courtyard collisions the move created. With
    reject_on_collision=True the board is NOT saved when the move would overlap
    another courtyard (within clearance_mm); the conflict is returned instead.
    """
    board = Board(board_path)
    result = board.move_footprint(reference, x_mm, y_mm, rotation_deg)
    collisions = board.collisions(references=[reference], clearance_mm=clearance_mm)
    result["collisions"] = collisions
    if reject_on_collision and collisions:
        result["saved"] = False
        result["rejected_due_to_collision"] = True
        return result
    result["backup"] = board.save()
    result["saved"] = True
    return result


@mcp.tool()
def move_footprints(board_path: str, moves: list[Move],
                    reject_on_collision: bool = False, clearance_mm: float = 0.0) -> dict:
    """Move several footprints in one batch, then save the board once.

    Preferred over repeated move_footprint calls: one backup, one write. The
    result always lists any courtyard collisions involving the moved parts.
    With reject_on_collision=True nothing is written if any move would overlap
    another courtyard (within clearance_mm) — the conflicts are returned so you
    can pick new positions and call again.
    """
    board = Board(board_path)
    results = [
        board.move_footprint(m.reference, m.x_mm, m.y_mm, m.rotation_deg) for m in moves
    ]
    moved_refs = [m.reference for m in moves]
    collisions = board.collisions(references=moved_refs, clearance_mm=clearance_mm)
    if reject_on_collision and collisions:
        return {"saved": False, "rejected_due_to_collision": True,
                "moved": results, "count": len(results), "collisions": collisions,
                "note": "nothing written; resolve the collisions or call again "
                        "without reject_on_collision"}
    backup = board.save()
    return {"saved": True, "moved": results, "count": len(results),
            "collisions": collisions, "backup": backup}


@mcp.tool()
def check_collisions(board_path: str, references: list[str] | None = None,
                     clearance_mm: float = 0.0) -> dict:
    """Footprint pairs whose courtyards overlap (or sit closer than clearance_mm).

    With clearance_mm=0 returns only true overlaps (a DRC error). gap_mm is the
    signed gap: negative means overlapping by that much. Pass references to
    limit results to pairs touching those footprints. Front and back courtyards
    are compared separately, so opposite-side parts don't count as colliding.
    """
    cols = Board(board_path).collisions(references, clearance_mm)
    return {"collision_count": len(cols), "clearance_mm": clearance_mm, "collisions": cols}


@mcp.tool()
def pad_position(board_path: str, reference: str, pad: str) -> dict:
    """Absolute position (mm), net, size, and angle of one pad, e.g. (U1, '28')."""
    return Board(board_path).pad_position(reference, pad)


@mcp.tool()
def net_endpoints(board_path: str, net: str) -> dict:
    """Every pad on a net with absolute positions, plus track-cluster count.

    Useful for routing planning: cluster_count > 1 means the net still has
    that many separate groups to join.
    """
    return Board(board_path).net_endpoints(net)


@mcp.tool()
def measure_placement_quality(board_path: str) -> dict:
    """Placement-quality components: ratsnest length, airwire and collision
    counts, and area utilisation — reported separately (not a blended score) so
    you can see which dimension changed between passes."""
    return Board(board_path).measure_placement_quality()


@mcp.tool()
def nearest_free_position(board_path: str, reference: str, anchor_pad: str,
                          min_clearance_mm: float = 0.2,
                          max_radius_mm: float = 40.0) -> dict:
    """Closest position to a pin where a footprint's courtyard is clear.

    anchor_pad is 'REF.PAD' (e.g. 'U1.28'). Searches outward from that pad and
    returns the nearest footprint origin (current rotation kept) where
    reference's courtyard clears every other footprint by min_clearance_mm.
    Does not move anything — feed the returned x_mm/y_mm to move_footprints.
    """
    if "." not in anchor_pad:
        raise ValueError("anchor_pad must be 'REF.PAD', e.g. 'U1.28'")
    aref, apad = anchor_pad.rsplit(".", 1)
    return Board(board_path).nearest_free_position(
        reference, aref, apad, min_clearance_mm=min_clearance_mm,
        max_radius_mm=max_radius_mm)


@mcp.tool()
def auto_place_decoupling(board_path: str, ic_reference: str, caps: list[str],
                          clearance_mm: float = 0.2) -> dict:
    """Place decoupling caps next to the IC pin each one shares a net with, then save.

    For every cap, picks an IC pad on a shared net (preferring a supply pin) and
    seats the cap's courtyard against the IC without overlapping anything already
    placed. Reports placed/unplaced caps and ratsnest length before vs after.
    Writes a .bak backup; refuses if the board is open in KiCad.
    """
    board = Board(board_path)
    result = board.auto_place_decoupling(ic_reference, caps, clearance_mm)
    result["backup"] = board.save()
    return result


@mcp.tool()
def add_rule_area(board_path: str, layers: str, polygon: list[list[float]],
                  keep_out_tracks: bool = True, keep_out_vias: bool = True,
                  keep_out_pads: bool = False, keep_out_copper: bool = True,
                  keep_out_footprints: bool = False, name: str | None = None) -> dict:
    """Add a keep-out rule area over a polygon, then save.

    layers is a comma list (e.g. 'F.Cu,B.Cu' or 'F&B.Cu'); polygon is [[x,y],...]
    in board mm. The keep_out_* flags choose what is disallowed inside the area.
    Writes a .bak backup; refuses if the board is open in KiCad.
    """
    board = Board(board_path)
    result = board.add_zone(
        polygon, layers, net=None, name=name,
        keepout={
            "tracks": keep_out_tracks, "vias": keep_out_vias, "pads": keep_out_pads,
            "copperpour": keep_out_copper, "footprints": keep_out_footprints,
        },
    )
    result["backup"] = board.save()
    return result


@mcp.tool()
def add_filled_zone(board_path: str, layers: str, net: str,
                    polygon: list[list[float]], name: str | None = None) -> dict:
    """Add a copper-pour zone on a net over a polygon, then save.

    layers is a comma list; polygon is [[x,y],...] in board mm. NOTE: viaduct
    writes the zone outline and settings but does NOT compute the copper fill —
    KiCad fills it when the board is next opened (or run_drc with
    refill_zones=True). Writes a .bak backup; refuses if open in KiCad.
    """
    board = Board(board_path)
    result = board.add_zone(polygon, layers, net=net, name=name)
    result["backup"] = board.save()
    return result


@mcp.tool()
def footprint_info(footprint_name: str) -> dict:
    """Dimensions, pad count, courtyard, and layers of a library footprint — without
    placing it. Accepts 'Library:Footprint' (e.g. 'Capacitor_SMD:C_0402_1005Metric')
    or a path to a .kicad_mod file. Set VIADUCT_FOOTPRINT_DIRS if your libraries
    are in a non-standard location."""
    return footprints.footprint_info(footprint_name)


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


@mcp.tool()
def backup_list(file_path: str) -> dict:
    """List available backups for a file: the last-edit .bak plus the numbered
    history (newest first). Each edit pushes a new history entry, so you can step
    back several edits with backup_restore_to."""
    return {"file": file_path, "backups": safety.list_backups(file_path)}


@mcp.tool()
def backup_restore_to(file_path: str, name: str) -> dict:
    """Restore a file from a specific backup (a name from backup_list), not just
    the most recent one. Refuses if the file is open in KiCad."""
    restored = safety.restore_to(file_path, name)
    return {"restored": restored, "from": name}


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
