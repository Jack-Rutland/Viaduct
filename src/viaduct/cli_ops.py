"""Operations that shell out to kicad-cli: checks, exports, renders."""

from __future__ import annotations

import json
import os
import tempfile

from . import kicad_cli, reports

DEFAULT_SVG_LAYERS = "F.Cu,B.Cu,F.SilkS,F.Mask,Edge.Cuts"


def _out_dir(source_file: str, output_dir: str | None) -> str:
    d = output_dir or os.path.join(os.path.dirname(os.path.abspath(source_file)), "viaduct_out")
    os.makedirs(d, exist_ok=True)
    return d


def _severity_flags(severity: str) -> list[str]:
    flags = {
        "all": ["--severity-all"],
        "error": ["--severity-error"],
        "warning": ["--severity-warning"],
    }
    if severity not in flags:
        raise ValueError(f"severity must be one of {sorted(flags)}, got {severity!r}")
    return flags[severity]


def run_drc(board_path: str, severity: str = "all", schematic_parity: bool = False,
            refill_zones: bool = False) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report = tmp.name
    try:
        args = ["pcb", "drc", "--format", "json", "--units", "mm",
                *_severity_flags(severity), "-o", report]
        if schematic_parity:
            args.append("--schematic-parity")
        if refill_zones:
            args.append("--refill-zones")
        kicad_cli.run([*args, board_path])
        with open(report, encoding="utf-8") as f:
            return reports.summarize_drc(json.load(f))
    finally:
        if os.path.exists(report):
            os.unlink(report)


def run_erc(schematic_path: str, severity: str = "all") -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report = tmp.name
    try:
        args = ["sch", "erc", "--format", "json", "--units", "mm",
                *_severity_flags(severity), "-o", report]
        kicad_cli.run([*args, schematic_path])
        with open(report, encoding="utf-8") as f:
            return reports.summarize_erc(json.load(f))
    finally:
        if os.path.exists(report):
            os.unlink(report)


def export_gerbers(board_path: str, output_dir: str | None = None,
                   include_drill: bool = True) -> dict:
    d = _out_dir(board_path, output_dir)
    before = set(os.listdir(d))
    kicad_cli.run(["pcb", "export", "gerbers", "-o", d, board_path])
    if include_drill:
        kicad_cli.run(["pcb", "export", "drill", "-o", d + os.sep, "--generate-map", board_path])
    produced = sorted(set(os.listdir(d)) - before) or sorted(os.listdir(d))
    return {"output_dir": d, "files": produced}


def export_bom(schematic_path: str, output_path: str | None = None,
               group_by: str = "Value,Footprint", exclude_dnp: bool = False) -> dict:
    out = output_path or os.path.join(
        _out_dir(schematic_path, None),
        os.path.basename(schematic_path).replace(".kicad_sch", "_bom.csv"),
    )
    args = ["sch", "export", "bom", "-o", out]
    if group_by:
        args += ["--group-by", group_by]
    if exclude_dnp:
        args.append("--exclude-dnp")
    kicad_cli.run([*args, schematic_path])
    with open(out, encoding="utf-8") as f:
        preview = f.read()
    return {"output_file": out, "csv": preview if len(preview) < 20000 else preview[:20000] + "\n..."}


def export_step(board_path: str, output_path: str | None = None) -> dict:
    out = output_path or os.path.join(
        _out_dir(board_path, None),
        os.path.basename(board_path).replace(".kicad_pcb", ".step"),
    )
    kicad_cli.run(["pcb", "export", "step", "--force", "--subst-models", "-o", out, board_path])
    return {"output_file": out, "size_bytes": os.path.getsize(out)}


def export_netlist(schematic_path: str, output_path: str | None = None,
                   format: str = "kicadsexpr") -> dict:
    ext = {"kicadsexpr": ".net", "kicadxml": ".xml", "spice": ".cir"}.get(format, ".net")
    out = output_path or os.path.join(
        _out_dir(schematic_path, None),
        os.path.basename(schematic_path).replace(".kicad_sch", ext),
    )
    kicad_cli.run(["sch", "export", "netlist", "--format", format, "-o", out, schematic_path])
    return {"output_file": out, "format": format, "size_bytes": os.path.getsize(out)}


def render_svg(board_path: str, output_path: str | None = None,
               layers: str = DEFAULT_SVG_LAYERS) -> dict:
    out = output_path or os.path.join(
        _out_dir(board_path, None),
        os.path.basename(board_path).replace(".kicad_pcb", ".svg"),
    )
    kicad_cli.run([
        "pcb", "export", "svg", "--mode-single", "-o", out,
        "--layers", layers, "--page-size-mode", "2", "--exclude-drawing-sheet",
        board_path,
    ])
    return {"output_file": out, "layers": layers, "size_bytes": os.path.getsize(out)}


def render_png(board_path: str, output_path: str | None = None, width: int = 1200,
               height: int = 900, side: str = "top", zoom: float = 0.9) -> str:
    out = output_path or os.path.join(
        _out_dir(board_path, None),
        os.path.basename(board_path).replace(".kicad_pcb", f"_{side}.png"),
    )
    kicad_cli.run([
        "pcb", "render", "-o", out, "--width", str(width), "--height", str(height),
        "--side", side, "--background", "opaque", "--quality", "basic",
        "--zoom", str(zoom), board_path,
    ], timeout=600)
    return out
