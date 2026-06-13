#!/usr/bin/env python3
"""Viaduct test script — verifies against the real KiCad installation.

Run:  python3 tests/test_viaduct.py

Needs KiCad installed (kicad-cli). Does NOT need the `mcp` package: it
exercises the core modules directly. The test project is copied to a temp
dir first, so the checked-in fixture is never modified.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from viaduct import cli_ops, kicad_cli, sexpr  # noqa: E402
from viaduct.board import Board  # noqa: E402
from viaduct.safety import BoardLockedError, lockfile_path, restore_backup  # noqa: E402
from viaduct.schematic import Schematic  # noqa: E402

PROJECT = os.path.join(os.path.dirname(__file__), "test_project")
PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")
    PASS += 1


def main():
    tmp = tempfile.mkdtemp(prefix="viaduct_test_")
    for f in os.listdir(PROJECT):
        shutil.copy2(os.path.join(PROJECT, f), tmp)
    pcb = os.path.join(tmp, "testboard.kicad_pcb")
    sch = os.path.join(tmp, "testboard.kicad_sch")

    print("== kicad-cli ==")
    ver = kicad_cli.version()
    check("kicad-cli found and runs", bool(re.match(r"\d+\.", ver)), ver)
    print(f"  KiCad {ver}")

    print("== s-expression round-trip ==")
    with open(pcb, encoding="utf-8") as f:
        original = f.read()
    tree = sexpr.parse(original)
    out = sexpr.dumps(tree)
    tree2 = sexpr.parse(out)
    check("parse(dumps(parse(x))) is stable", sexpr.dumps(tree2) == out)
    # KiCad must accept our re-serialized output: run DRC on it
    rt = os.path.join(tmp, "roundtrip.kicad_pcb")
    with open(rt, "w", encoding="utf-8") as f:
        f.write(out)
    drc_orig = cli_ops.run_drc(pcb)
    drc_rt = cli_ops.run_drc(rt)
    check("KiCad accepts round-tripped board (DRC runs)", drc_rt["violation_count"] >= 0)
    check(
        "round-trip DRC identical to original",
        (drc_rt["violation_count"], drc_rt["unconnected_count"], drc_rt["by_type"])
        == (drc_orig["violation_count"], drc_orig["unconnected_count"], drc_orig["by_type"]),
        f"{drc_rt['by_type']} vs {drc_orig['by_type']}",
    )

    print("== board inspection ==")
    board = Board(pcb)
    info = board.board_info()
    check("board_info layers", "F.Cu" in info["copper_layers"])
    check("board_info bbox", info["board_bbox_mm"]["width"] == 35.0, str(info["board_bbox_mm"]))
    check("board_info counts", info["footprint_count"] == 5 and info["via_count"] == 0)
    fps = board.list_footprints()
    check("list_footprints", {f["reference"] for f in fps} == {"U1", "C1", "C2", "R1", "J1"})
    j1 = next(f for f in fps if f["reference"] == "J1")
    check("footprint rotation read", j1["rotation_deg"] == 90.0)
    nets = board.list_nets()
    check("list_nets", set(nets) == {"GND", "VCC", "IN", "OUT"}, str(nets))
    rules = board.design_rules()
    check("design_rules from .kicad_pro", rules["rules"].get("min_clearance") == 0.2)
    check("design_rules net classes", any(c["name"] == "Power" for c in rules["net_classes"]))

    print("== pad positions vs KiCad (IPC-D-356 ground truth) ==")
    # kicad-cli writes pad coordinates into the IPC-D-356 netlist; compare.
    ipc = os.path.join(tmp, "test.d356")
    kicad_cli.run(["pcb", "export", "ipcd356", "-o", ipc, pcb])
    truth = parse_ipcd356(ipc)
    pads = {(p["reference"], p["pad"]): (p["x_mm"], p["y_mm"]) for p in board.pads()}
    compared = 0
    for (ref, pad_num), (tx, ty) in truth.items():
        if (ref, pad_num) in pads:
            x, y = pads[(ref, pad_num)]
            ok = math.isclose(x, tx, abs_tol=0.01) and math.isclose(y, -ty, abs_tol=0.01)
            if not ok:
                check(f"pad {ref}.{pad_num} position", False, f"ours ({x},{y}) vs ipc ({tx},{-ty})")
            compared += 1
    check(f"pad positions match IPC-D-356 ({compared} pads incl. rotated J1)", compared >= 10)

    print("== connectivity / courtyards / ratsnest ==")
    conn = board.connectivity(["J1"])
    j1_pads = {p["pad"]: p for p in conn[0]["pads"]}
    check("connectivity rotated pad abs pos", j1_pads["2"]["x_mm"] == 42.54 and j1_pads["2"]["y_mm"] == 52.0)
    check("connectivity nets", j1_pads["1"]["net"] == "VCC" and j1_pads["2"]["net"] == "GND")
    cy = {c["reference"]: c for c in board.courtyards()}
    u1 = cy["U1"]["courtyards"][0]
    check("courtyard bbox U1", u1["width_mm"] == 7.4 and u1["height_mm"] == 5.4, str(u1))
    j1cy = cy["J1"]["courtyards"][0]
    # J1 courtyard is rotated 90deg: 3.6 x 6.15 becomes 6.15 x 3.6
    check("courtyard bbox rotated J1", abs(j1cy["width_mm"] - 6.15) < 1e-6 and abs(j1cy["height_mm"] - 3.6) < 1e-6, str(j1cy))
    rn = board.ratsnest()
    check("ratsnest finds airwires", rn["airwire_count"] > 0, str(rn))
    nets_in_rn = {a["net"] for a in rn["airwires"]}
    check("ratsnest covers unrouted nets", {"GND", "IN", "OUT"} <= nets_in_rn, str(nets_in_rn))
    # The dangling VCC track connects nothing, so VCC still needs airwires
    # between its three pads; but C1.1<->track are in one cluster already.
    vcc_wires = [a for a in rn["airwires"] if a["net"] == "VCC"]
    check("ratsnest VCC airwires", len(vcc_wires) == 2, str(vcc_wires))
    # DRC agrees on what's unconnected: same number of missing connections
    check(
        "ratsnest airwire count matches DRC unconnected count",
        rn["airwire_count"] == drc_orig["unconnected_count"],
        f"{rn['airwire_count']} vs {drc_orig['unconnected_count']}",
    )

    print("== editing: move + rotate, then KiCad must accept the file ==")
    before = board.ratsnest()["total_length_mm"]
    r = board.move_footprint("C1", 54.5, 49.0, 90.0)
    check("move_footprint reports old/new", r["from"]["x_mm"] == 62.0 and r["to"]["rotation_deg"] == 90.0)
    board.move_footprint("C2", 46.0, 56.5)
    board.move_footprint("R1", 53.0, 56.5, 180.0)
    bak = board.save()
    check("backup written", os.path.isfile(bak) and bak.endswith(".bak"))
    moved = Board(pcb)
    c1 = next(f for f in moved.list_footprints() if f["reference"] == "C1")
    check("move persisted", c1["x_mm"] == 54.5 and c1["rotation_deg"] == 90.0)
    after = moved.ratsnest()["total_length_mm"]
    check("placement improved ratsnest", after < before, f"{after} !< {before}")
    # pad angle quirk: C1 pads must now carry the footprint angle
    ipc2 = os.path.join(tmp, "test2.d356")
    kicad_cli.run(["pcb", "export", "ipcd356", "-o", ipc2, pcb])
    truth2 = parse_ipcd356(ipc2)
    ours2 = {(p["reference"], p["pad"]): (p["x_mm"], p["y_mm"]) for p in moved.pads()}
    tx, ty = truth2[("C1", "1")]
    ox, oy = ours2[("C1", "1")]
    check("rotated pad position matches KiCad after edit",
          math.isclose(ox, tx, abs_tol=0.01) and math.isclose(oy, -ty, abs_tol=0.01),
          f"ours ({ox},{oy}) vs ipc ({tx},{-ty})")
    drc_after = cli_ops.run_drc(pcb)
    check("KiCad accepts edited board (DRC runs)", drc_after["violation_count"] >= 0)

    print("== board outline ==")
    moved.set_board_outline_rect(34, 41, 38, 30)
    moved.save()
    info2 = Board(pcb).board_info()
    check("outline replaced", info2["board_bbox_mm"]["width"] == 38.0, str(info2["board_bbox_mm"]))
    check("KiCad accepts new outline", cli_ops.run_drc(pcb)["violation_count"] >= 0)

    print("== safety: lockfile + restore ==")
    lck = lockfile_path(pcb)
    with open(lck, "w") as f:
        f.write("{}")
    locked_board = Board(pcb)
    locked_board.move_footprint("C1", 1.0, 1.0)
    try:
        locked_board.save()
        check("refuses to save while locked", False)
    except BoardLockedError:
        check("refuses to save while locked", True)
    os.unlink(lck)
    restore_backup(pcb)
    restored = Board(pcb)
    check("restore_backup works", Board(pcb).board_info()["board_bbox_mm"]["width"] == 35.0)
    del restored

    print("== schematic ==")
    s = Schematic(sch)
    syms = s.list_symbols()
    check("list_symbols", {x["reference"] for x in syms} == {"R1", "C1"})
    check("symbol footprints", next(x for x in syms if x["reference"] == "R1")["footprint"]
          == "Resistor_SMD:R_0603_1608Metric")
    labels = s.list_labels()
    check("list_labels", {(l["kind"], l["text"]) for l in labels}
          == {("label", "OUT"), ("global_label", "GND")})
    r = s.set_symbol_property("R1", "Value", "22k")
    check("set_symbol_property existing", r["old"] == "10k")
    s.set_symbol_property("R1", "MPN", "RC0603FR-0722KL")
    s.save()
    s2 = Schematic(sch)
    r1 = next(x for x in s2.list_symbols() if x["reference"] == "R1")
    check("property edits persisted", r1["value"] == "22k" and r1["properties"]["MPN"] == "RC0603FR-0722KL")
    erc = cli_ops.run_erc(sch)
    check("KiCad accepts edited schematic (ERC runs)", erc["violation_count"] >= 0)
    check("ERC summary shape", "by_severity" in erc and isinstance(erc["violations"], list))

    print("== ERC / DRC summaries ==")
    check("DRC summary types", "track_dangling" in drc_orig["by_type"], str(drc_orig["by_type"]))
    v = drc_orig["violations"][0]
    check("DRC violation has position in mm",
          isinstance(v["items"][0]["x_mm"], (int, float)) and drc_orig["units"] == "mm")

    print("== exports / renders ==")
    g = cli_ops.export_gerbers(pcb)
    check("gerbers + drill produced", any(f.endswith(".gtl") or "F_Cu" in f for f in g["files"])
          and any(f.endswith(".drl") for f in g["files"]), str(g["files"]))
    n = cli_ops.export_netlist(sch)
    check("netlist export", os.path.getsize(n["output_file"]) > 0)
    b = cli_ops.export_bom(sch)
    check("bom export", "R1" in b["csv"] and "22k" in b["csv"])
    svg = cli_ops.render_svg(pcb)
    check("svg render", svg["size_bytes"] > 1000)
    png = cli_ops.render_png(pcb, width=800, height=600)
    check("png render", os.path.getsize(png) > 5000)
    st = cli_ops.export_step(pcb)
    check("step export", st["size_bytes"] > 1000)

    print("== targeted queries: pad_position / net_endpoints ==")
    fresh = Board(pcb_fresh(tmp, "board2"))
    pp = fresh.pad_position("U1", "8")
    check("pad_position net + coords", pp["net"] == "VCC" and isinstance(pp["x_mm"], float))
    try:
        fresh.pad_position("U1", "999")
        check("pad_position missing raises", False)
    except KeyError:
        check("pad_position missing raises", True)
    ne = fresh.net_endpoints("GND")
    check("net_endpoints lists pads", ne["pad_count"] >= 3 and "U1.4" in {p["pad"] for p in ne["pads"]})

    print("== collision detection (SAT courtyards) ==")
    check("no baseline collisions", fresh.collisions() == [])
    c1 = next(f for f in fresh.list_footprints() if f["reference"] == "C1")
    fresh.move_footprint("C2", c1["x_mm"], c1["y_mm"])  # stack C2 on C1
    cols = fresh.collisions(references=["C2"])
    check("overlap detected", len(cols) == 1 and cols[0]["overlap"] and cols[0]["gap_mm"] < 0,
          str(cols))
    check("collision side reported", cols[0]["side"] == "front")

    print("== nearest_free_position / auto_place_decoupling ==")
    b3path = pcb_fresh(tmp, "board3")
    b = Board(b3path)
    nf = b.nearest_free_position("C1", "U1", "8", min_clearance_mm=0.3)
    check("nearest_free_position returns a clear spot",
          nf["x_mm"] is not None and nf["min_gap_mm"] >= 0.3 - 1e-6, str(nf))
    ap = b.auto_place_decoupling("U1", ["C1", "C2"], clearance_mm=0.3)
    check("auto_place placed both caps", len(ap["placed"]) == 2 and not ap["unplaced"], str(ap))
    check("auto_place improved ratsnest",
          ap["ratsnest_after_mm"] < ap["ratsnest_before_mm"],
          f"{ap['ratsnest_after_mm']} !< {ap['ratsnest_before_mm']}")
    check("auto_place left no collisions", b.collisions() == [], str(b.collisions()))
    b.save()
    check("KiCad accepts auto-placed board", cli_ops.run_drc(b3path)["violation_count"] >= 0)
    mq = Board(b3path).measure_placement_quality()
    check("measure_placement_quality components",
          mq["courtyard_collision_count"] == 0 and "ratsnest_total_mm" in mq
          and "area_utilization" in mq)

    print("== zones / rule areas (KiCad must accept the file) ==")
    b = Board(b3path)
    b.add_zone([[40, 48], [50, 48], [50, 58], [40, 58]], "F.Cu",
               keepout={"tracks": True, "vias": True, "copperpour": True}, name="cutout")
    b.add_zone([[30, 30], [80, 30], [80, 70], [30, 70]], "B.Cu", net="GND", name="gnd_pour")
    b.save()
    check("zones added to board", Board(b3path).board_info()["zone_count"] >= 2)
    check("KiCad accepts board with new zones", cli_ops.run_drc(b3path)["violation_count"] >= 0)

    print("== multi-backup history ==")
    from viaduct.safety import list_backups, restore_to
    baks = list_backups(b3path)
    check("backup history + last-edit recorded",
          any(x["kind"] == "history" for x in baks)
          and any(x["kind"] == "last_edit" for x in baks), str(baks))
    oldest = [x for x in baks if x["kind"] == "history"][-1]["name"]
    restore_to(b3path, oldest)
    check("restore_to an older backup runs", cli_ops.run_drc(b3path)["violation_count"] >= 0)
    try:
        restore_to(b3path, "../etc/passwd")
        check("restore_to rejects foreign names", False)
    except Exception:
        check("restore_to rejects foreign names", True)

    print("== DRC summary / detail shaping ==")
    from viaduct import reports
    full = cli_ops.run_drc(pcb)
    summ = reports.compact_drc(full, "summary", top_n=3)
    check("drc summary trims lists",
          "worst" in summ and "violations" not in summ and len(summ["worst"]) <= 3)
    check("drc full passthrough", reports.compact_drc(full, "full") is full)
    if full["by_type"]:
        t = next(iter(full["by_type"]))
        det = reports.compact_drc(full, t)
        check("drc detail filters by type",
              det["count"] >= 1 and all(v["type"] == t for v in det["violations"]))

    print("== footprint_info (library lookup) ==")
    from viaduct import footprints
    try:
        fi = footprints.footprint_info("Resistor_SMD:R_0603_1608Metric")
        check("footprint_info pad count", fi["pad_count"] == 2, str(fi))
        check("footprint_info courtyard size",
              fi["courtyard"] is not None and fi["courtyard"]["width_mm"] > 0)
        check("footprint_info smd flag", fi["smd"] is True)
    except FileNotFoundError as e:
        print(f"  [skip] standard footprint library not found ({e})")

    print("== MCP server (optional, needs `mcp` package) ==")
    try:
        import mcp  # noqa: F401
        from viaduct.server import mcp as server
        import asyncio
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        expected = {
            "kicad_version", "run_erc", "run_drc", "board_info", "list_footprints",
            "list_nets", "connectivity", "courtyards", "ratsnest", "design_rules",
            "move_footprint", "move_footprints", "set_board_outline_rect",
            "list_symbols", "list_labels", "set_symbol_property", "export_gerbers",
            "export_bom", "export_step", "export_netlist", "render_board_svg",
            "render_board_png", "restore_backup",
            # collision-aware placement + queries
            "check_collisions", "pad_position", "net_endpoints",
            "measure_placement_quality", "nearest_free_position",
            "auto_place_decoupling", "add_rule_area", "add_filled_zone",
            "footprint_info", "backup_list", "backup_restore_to",
        }
        check("all 34 tools registered", expected <= names, str(expected - names))
    except ImportError:
        print("  [skip] mcp package not installed; server registration not tested")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nAll {PASS} checks passed (KiCad {ver}).")


def pcb_fresh(tmp: str, name: str) -> str:
    """Copy the pristine fixture board to tmp/<name>.kicad_pcb and return its path.

    Used by the collision/placement tests that mutate a board, so they start
    from a clean layout independent of the edits earlier sections made.
    """
    dst = os.path.join(tmp, f"{name}.kicad_pcb")
    shutil.copy2(os.path.join(PROJECT, "testboard.kicad_pcb"), dst)
    return dst


def parse_ipcd356(path: str) -> dict:
    """Pad records: 317/327 lines with refdes, pad number, X/Y in 1/10000 inch."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith(("317", "327")):
                continue
            m = re.search(r"\s(\S+)\s+-(\S+)\s.*X([+-]\d+)Y([+-]\d+)", line)
            if not m:
                continue
            ref, pad_num, x, y = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            out[(ref, pad_num)] = (x * 0.0254 / 10.0, y * 0.0254 / 10.0)
    return out


if __name__ == "__main__":
    main()
