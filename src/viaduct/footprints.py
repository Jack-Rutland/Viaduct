"""Inspect footprint definitions (.kicad_mod) from KiCad libraries, without
placing them on a board.

A ``.kicad_mod`` file is a single ``(footprint "Name" ...)`` s-expression — the
same shape as a footprint embedded in a board, but unplaced (origin 0,0,0). We
reuse the board module's geometry helpers to report pad count, courtyard size,
and overall extents so the caller can decide whether a part fits before
dropping it onto a board.
"""

from __future__ import annotations

import math
import os

from . import sexpr
from .board import FP_SHAPES, _full_shape_points
from .sexpr import child, children, to_float

# Default locations searched for <Library>.pretty/<Footprint>.kicad_mod, after
# any dirs from VIADUCT_FOOTPRINT_DIRS or KiCad's *_FOOTPRINT_DIR env vars.
_DEFAULT_DIRS = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    r"C:\Program Files\KiCad\share\kicad\footprints",
)


def library_dirs() -> list[str]:
    dirs: list[str] = []
    env = os.environ.get("VIADUCT_FOOTPRINT_DIRS")
    if env:
        dirs += [d for d in env.split(os.pathsep) if d]
    for key, val in os.environ.items():
        if key.endswith("_FOOTPRINT_DIR") and val:
            dirs.append(val)
    dirs += list(_DEFAULT_DIRS)
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def resolve_footprint_file(name: str) -> str:
    """Locate the .kicad_mod for a 'Library:Footprint' id or a direct path."""
    if name.endswith(".kicad_mod") and os.path.isfile(name):
        return os.path.abspath(name)
    if ":" not in name:
        raise ValueError(
            "footprint name must be 'Library:Footprint' (e.g. "
            "'Capacitor_SMD:C_0402_1005Metric') or a path to a .kicad_mod file"
        )
    lib, fp = name.split(":", 1)
    searched = library_dirs()
    for d in searched:
        cand = os.path.join(d, lib + ".pretty", fp + ".kicad_mod")
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"footprint {name!r} not found. Searched: {', '.join(searched) or '(no library dirs found)'}. "
        "Set VIADUCT_FOOTPRINT_DIRS to point at your KiCad footprint libraries."
    )


def _bare_field(node, name: str) -> str:
    n = child(node, name)
    return str(n[1]) if n and len(n) > 1 else ""


def footprint_info(name: str) -> dict:
    """Dimensions, pad count, courtyard, and layers of a footprint — unplaced."""
    path = resolve_footprint_file(name)
    with open(path, encoding="utf-8") as f:
        node = sexpr.parse(f.read())

    def prop(pname: str) -> str:
        for p in children(node, "property"):
            if len(p) >= 3 and p[1] == pname:
                return str(p[2])
        return ""

    overall = [math.inf, math.inf, -math.inf, -math.inf]
    crt = [math.inf, math.inf, -math.inf, -math.inf]
    crt_found = False
    pads = children(node, "pad")
    pad_info = []
    for pad in pads:
        at = child(pad, "at")
        px, py = (to_float(at[1]), to_float(at[2])) if at else (0.0, 0.0)
        size = child(pad, "size")
        sx = to_float(size[1]) if size else 0.0
        sy = to_float(size[2]) if size and len(size) > 2 else sx
        pad_info.append({
            "pad": str(pad[1]) if len(pad) > 1 else "",
            "type": str(pad[2]) if len(pad) > 2 else "",
            "x_mm": round(px, 4),
            "y_mm": round(py, 4),
            "size_mm": [sx, sy],
        })
        overall[0] = min(overall[0], px - sx / 2)
        overall[1] = min(overall[1], py - sy / 2)
        overall[2] = max(overall[2], px + sx / 2)
        overall[3] = max(overall[3], py + sy / 2)

    layers_used = set()
    for kind in FP_SHAPES:
        for shape in children(node, kind):
            layer = child(shape, "layer")
            lname = str(layer[1]) if layer else ""
            layers_used.add(lname)
            on_crtyd = lname.endswith(".CrtYd")
            for x, y in _full_shape_points(shape, kind.replace("fp_", "")):
                overall[0] = min(overall[0], x)
                overall[1] = min(overall[1], y)
                overall[2] = max(overall[2], x)
                overall[3] = max(overall[3], y)
                if on_crtyd:
                    crt_found = True
                    crt[0] = min(crt[0], x)
                    crt[1] = min(crt[1], y)
                    crt[2] = max(crt[2], x)
                    crt[3] = max(crt[3], y)

    def box(b):
        return {
            "width_mm": round(b[2] - b[0], 4),
            "height_mm": round(b[3] - b[1], 4),
            "min_x_mm": round(b[0], 4),
            "min_y_mm": round(b[1], 4),
            "max_x_mm": round(b[2], 4),
            "max_y_mm": round(b[3], 4),
        }

    return {
        "name": name,
        "file": path,
        "description": prop("Description") or _bare_field(node, "descr"),
        "tags": prop("ki_keywords") or _bare_field(node, "tags"),
        "pad_count": len(pads),
        "smd": bool(pad_info) and all(p["type"] == "smd" for p in pad_info),
        "courtyard": box(crt) if crt_found else None,
        "bounding_box": box(overall) if overall[0] != math.inf else None,
        "layers": sorted(l for l in layers_used if l),
        "pads": pad_info,
    }
