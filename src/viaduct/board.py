"""Read and edit .kicad_pcb files.

All coordinates in and out of this module are millimetres in the KiCad board
frame (X right, Y *down*). Rotations are degrees, counter-clockwise as
displayed in the PCB editor.

Format notes (verified against KiCad 10.0 output):

- A pad's ``(at x y angle)`` offset is in the *unrotated* footprint frame,
  but the stored angle is the SUM of the pad's own rotation and the
  footprint rotation. Rotating a footprint therefore means rewriting every
  pad/text angle by the rotation delta while leaving x/y offsets alone.
- Absolute pad position for a footprint at (fx, fy) rotated t degrees:
  ax = fx + px*cos(t) + py*sin(t);  ay = fy - px*sin(t) + py*cos(t)
  (verified against kicad-cli DRC / IPC-D-356 output).
- KiCad <= 9 keeps a top-level net table ``(net N "name")`` and pads refer
  to nets as ``(net N "name")``; KiCad 10 (version 20260206) drops numbers
  entirely and pads carry ``(net "name")``. Both forms are handled.
"""

from __future__ import annotations

import json
import math
import os
import uuid as uuid_mod

from . import sexpr
from .safety import guarded_write
from .sexpr import Sym, child, children, is_node, num, to_float

FP_SHAPES = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")
GR_SHAPES = ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve")


def _get_at(node):
    """(x, y, angle_degrees) from a node's (at ...) child."""
    at = child(node, "at")
    if at is None:
        return 0.0, 0.0, 0.0
    vals = [to_float(a) for a in at[1:4]]
    while len(vals) < 3:
        vals.append(0.0)
    return vals[0], vals[1], vals[2]


def _set_at(node, x, y, angle):
    at = child(node, "at")
    new = ["at", num(round(x, 6)), num(round(y, 6))]
    if angle:
        new.append(num(round(angle, 6)))
    new[0] = Sym("at")
    if at is None:
        node.append(new)
    else:
        at[:] = new


def _norm_angle(a: float) -> float:
    a = a % 360.0
    if a > 180.0:
        a -= 360.0
    return round(a, 6)


def _rot(px: float, py: float, deg: float) -> tuple[float, float]:
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return px * c + py * s, -px * s + py * c


def _new_uuid_node():
    return [Sym("uuid"), str(uuid_mod.uuid4())]


def _full_shape_points(shape, kind: str) -> list[tuple[float, float]]:
    """All corner/vertex points of a graphic shape in its own frame.

    Unlike :meth:`Board._shape_points` (which returns just the extreme
    points, enough for a bounding box), this expands rectangles to four
    corners so the result is a real polygon usable for collision tests.
    """
    def pt(name):
        n = child(shape, name)
        return (to_float(n[1]), to_float(n[2])) if n else None

    if kind == "rect":
        s, e = pt("start"), pt("end")
        if not s or not e:
            return []
        return [(s[0], s[1]), (e[0], s[1]), (e[0], e[1]), (s[0], e[1])]
    if kind == "line":
        return [p for p in (pt("start"), pt("end")) if p is not None]
    if kind == "arc":
        return [p for p in (pt("start"), pt("mid"), pt("end")) if p is not None]
    if kind == "circle":
        c, e = pt("center"), pt("end")
        if not c or not e:
            return []
        r = math.dist(c, e)
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] - r),
                (c[0] + r, c[1] + r), (c[0] - r, c[1] + r)]
    if kind in ("poly", "curve"):
        pts_node = child(shape, "pts")
        out = []
        if pts_node:
            for xy in children(pts_node, "xy"):
                out.append((to_float(xy[1]), to_float(xy[2])))
        return out
    return []


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull (counter-clockwise) via Andrew's monotone chain."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _poly_separation(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """Signed separation between two convex polygons (separating-axis theorem).

    Positive = the closest gap between them (millimetres); negative = they
    overlap, magnitude is the penetration depth. Candidate axes are the edge
    normals of both polygons, which is exact for convex shapes. Courtyards are
    convex rectangles in practice; a non-convex courtyard is approximated by
    its convex hull before this is called.
    """
    best = -math.inf
    for poly in (a, b):
        n = len(poly)
        for k in range(n):
            x1, y1 = poly[k]
            x2, y2 = poly[(k + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)
            length = math.hypot(nx, ny)
            if length == 0:
                continue
            nx, ny = nx / length, ny / length
            aproj = [nx * px + ny * py for px, py in a]
            bproj = [nx * px + ny * py for px, py in b]
            overlap = min(max(aproj), max(bproj)) - max(min(aproj), min(bproj))
            if -overlap > best:
                best = -overlap
    return best


def _poly_area(poly: list[tuple[float, float]]) -> float:
    """Absolute area of a simple polygon (shoelace formula)."""
    n = len(poly)
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _point_in_poly(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon for an arbitrary simple polygon."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Do segments p1-p2 and p3-p4 properly intersect?"""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _polys_intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    """True if two simple polygons overlap at all (edge cross or containment).

    Works for non-convex polygons, unlike :func:`_poly_separation` (which is
    convex-only but also reports a signed gap). Use this for keep-out zones and
    board cut-outs, whose outlines need not be convex.
    """
    na, nb = len(a), len(b)
    for i in range(na):
        for j in range(nb):
            if _segments_cross(a[i], a[(i + 1) % na], b[j], b[(j + 1) % nb]):
                return True
    return _point_in_poly(a[0], b) or _point_in_poly(b[0], a)


def _poly_inside(inner: list[tuple[float, float]], outer: list[tuple[float, float]]) -> bool:
    """True if *inner* lies wholly inside *outer* (vertices in, no edge crossings)."""
    if not all(_point_in_poly(p, outer) for p in inner):
        return False
    ni, no = len(inner), len(outer)
    for i in range(ni):
        for j in range(no):
            if _segments_cross(inner[i], inner[(i + 1) % ni],
                               outer[j], outer[(j + 1) % no]):
                return False
    return True


def _arc_length(p1, p2, p3) -> float:
    """Length of the circular arc through three points (chord if collinear)."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return math.dist(p1, p3)
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a1 = math.atan2(ay - uy, ax - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    am = math.atan2(by - uy, bx - ux)

    def norm(a):
        while a < 0:
            a += 2 * math.pi
        return a

    sweep = norm(a2 - a1)
    if norm(am - a1) > sweep:
        sweep -= 2 * math.pi
    return abs(r * sweep)


def _stitch_loops(segs: list[tuple], tol: float = 0.01) -> list[list[tuple[float, float]]]:
    """Stitch (start, end, points) segments into closed loops by matching endpoints."""
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    remaining = list(segs)
    loops = []
    while remaining:
        start, end, pts = remaining.pop(0)
        chain = list(pts)
        changed = True
        while changed and key(chain[0]) != key(chain[-1]):
            changed = False
            for i, (s, e, p) in enumerate(remaining):
                if key(e) == key(chain[-1]):
                    s, e, p = e, s, list(reversed(p))
                if key(s) == key(chain[-1]):
                    chain.extend(p[1:])
                    remaining.pop(i)
                    changed = True
                    break
        if len(chain) >= 4 and key(chain[0]) == key(chain[-1]):
            loops.append(chain[:-1])
    return loops


class Board:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        if not self.path.endswith(".kicad_pcb"):
            raise ValueError(f"not a .kicad_pcb file: {path}")
        with open(self.path, encoding="utf-8") as f:
            self.root = sexpr.parse(f.read())
        if not is_node(self.root, "kicad_pcb"):
            raise ValueError(f"{path} does not look like a KiCad board file")

    # -- file-level -------------------------------------------------------

    def save(self) -> str:
        """Write back (lockfile check + .bak backup). Returns backup path."""
        return guarded_write(self.path, sexpr.dumps(self.root))

    @property
    def version(self) -> int:
        v = child(self.root, "version")
        return int(v[1]) if v else 0

    # -- nets -------------------------------------------------------------

    def _net_table(self) -> dict[str, str]:
        """net number (as str) -> name, from the top-level table (pre-v10)."""
        table = {}
        for n in children(self.root, "net"):
            atoms = [a for a in n[1:] if not isinstance(a, list)]
            if len(atoms) >= 2:
                table[str(atoms[0])] = str(atoms[1])
        return table

    def _net_name(self, net_node) -> str:
        """Resolve a (net ...) reference on a pad/track to a net name."""
        if net_node is None:
            return ""
        atoms = [a for a in net_node[1:] if not isinstance(a, list)]
        if not atoms:
            return ""
        if len(atoms) >= 2:  # (net N "name")
            return str(atoms[1])
        a = atoms[0]
        if isinstance(a, Sym):  # (net N) by number only — look up the table
            return self._net_table().get(str(a), str(a))
        return str(a)  # (net "name"), KiCad 10

    def list_nets(self) -> list[str]:
        names = set(self._net_table().values())
        for fp in self.footprint_nodes():
            for pad in children(fp, "pad"):
                names.add(self._net_name(child(pad, "net")))
        for kind in ("segment", "arc", "via", "zone"):
            for t in children(self.root, kind):
                names.add(self._net_name(child(t, "net")))
        names.discard("")
        return sorted(names)

    # -- footprints ---------------------------------------------------------

    def footprint_nodes(self):
        return children(self.root, "footprint")

    def _fp_property(self, fp, name: str) -> str:
        for p in children(fp, "property"):
            if len(p) >= 3 and p[1] == name:
                return str(p[2])
        # very old files use (fp_text reference "U1" ...)
        for t in children(fp, "fp_text"):
            if len(t) >= 3 and t[1] == name.lower():
                return str(t[2])
        return ""

    def fp_ref(self, fp) -> str:
        return self._fp_property(fp, "Reference")

    def find_footprint(self, ref: str):
        for fp in self.footprint_nodes():
            if self.fp_ref(fp) == ref:
                return fp
        raise KeyError(
            f"no footprint with reference {ref!r} in {os.path.basename(self.path)} "
            f"(have: {', '.join(sorted(self.fp_ref(f) for f in self.footprint_nodes()))})"
        )

    def list_footprints(self) -> list[dict]:
        out = []
        for fp in self.footprint_nodes():
            x, y, rot = _get_at(fp)
            layer = child(fp, "layer")
            attr = child(fp, "attr")
            out.append(
                {
                    "reference": self.fp_ref(fp),
                    "value": self._fp_property(fp, "Value"),
                    "footprint": str(fp[1]) if len(fp) > 1 and not isinstance(fp[1], list) else "",
                    "layer": str(layer[1]) if layer else "F.Cu",
                    "x_mm": x,
                    "y_mm": y,
                    "rotation_deg": rot,
                    "type": " ".join(str(a) for a in attr[1:]) if attr else "",
                    "pad_count": len(children(fp, "pad")),
                    "description": self._fp_property(fp, "Description"),
                }
            )
        return out

    # -- pads / connectivity ------------------------------------------------

    def pads(self) -> list[dict]:
        """Every pad with its absolute position and net."""
        out = []
        for fp in self.footprint_nodes():
            fx, fy, frot = _get_at(fp)
            ref = self.fp_ref(fp)
            fp_layer = child(fp, "layer")
            for pad in children(fp, "pad"):
                px, py, pang = _get_at(pad)
                dx, dy = _rot(px, py, frot)
                size = child(pad, "size")
                sx = to_float(size[1]) if size else 0.0
                sy = to_float(size[2]) if size and len(size) > 2 else sx
                layers_node = child(pad, "layers")
                out.append(
                    {
                        "reference": ref,
                        "pad": str(pad[1]),
                        "kind": str(pad[2]) if len(pad) > 2 else "",
                        "net": self._net_name(child(pad, "net")),
                        "x_mm": round(fx + dx, 4),
                        "y_mm": round(fy + dy, 4),
                        "angle_deg": pang,
                        "size_mm": [sx, sy],
                        "layers": [str(a) for a in layers_node[1:]] if layers_node else [],
                        "footprint_layer": str(fp_layer[1]) if fp_layer else "F.Cu",
                    }
                )
        return out

    def connectivity(self, references: list[str] | None = None) -> list[dict]:
        """Pads grouped per footprint: net name + absolute position each."""
        wanted = set(references) if references else None
        by_ref: dict[str, list] = {}
        for p in self.pads():
            if wanted is not None and p["reference"] not in wanted:
                continue
            by_ref.setdefault(p["reference"], []).append(
                {
                    "pad": p["pad"],
                    "net": p["net"],
                    "x_mm": p["x_mm"],
                    "y_mm": p["y_mm"],
                }
            )
        if wanted:
            missing = wanted - set(by_ref)
            if missing:
                raise KeyError(f"footprint reference(s) not found: {', '.join(sorted(missing))}")
        return [{"reference": r, "pads": ps} for r, ps in sorted(by_ref.items())]

    # -- courtyards -----------------------------------------------------------

    def courtyards(self) -> list[dict]:
        out = []
        for fp in self.footprint_nodes():
            fx, fy, frot = _get_at(fp)
            boxes: dict[str, list] = {}
            for kind in FP_SHAPES:
                for shape in children(fp, kind):
                    layer = child(shape, "layer")
                    lname = str(layer[1]) if layer else ""
                    if not lname.endswith(".CrtYd"):
                        continue
                    pts = self._shape_points(shape, kind.replace("fp_", ""))
                    if not pts:
                        continue
                    box = boxes.setdefault(lname, [math.inf, math.inf, -math.inf, -math.inf])
                    for px, py in pts:
                        dx, dy = _rot(px, py, frot)
                        ax, ay = fx + dx, fy + dy
                        box[0] = min(box[0], ax)
                        box[1] = min(box[1], ay)
                        box[2] = max(box[2], ax)
                        box[3] = max(box[3], ay)
            entry = {"reference": self.fp_ref(fp), "courtyards": []}
            for lname, b in boxes.items():
                entry["courtyards"].append(
                    {
                        "layer": lname,
                        "min_x_mm": round(b[0], 4),
                        "min_y_mm": round(b[1], 4),
                        "max_x_mm": round(b[2], 4),
                        "max_y_mm": round(b[3], 4),
                        "width_mm": round(b[2] - b[0], 4),
                        "height_mm": round(b[3] - b[1], 4),
                    }
                )
            out.append(entry)
        return out

    def _shape_points(self, shape, kind: str) -> list[tuple[float, float]]:
        """Bounding points of a graphic shape, in its own coordinate frame."""
        def pt(name):
            n = child(shape, name)
            return (to_float(n[1]), to_float(n[2])) if n else None

        if kind in ("line", "rect"):
            pts = [pt("start"), pt("end")]
        elif kind == "arc":
            pts = [pt("start"), pt("mid"), pt("end")]
        elif kind == "circle":
            c, e = pt("center"), pt("end")
            if not c or not e:
                return []
            r = math.dist(c, e)
            return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        elif kind in ("poly", "curve"):
            pts_node = child(shape, "pts")
            pts = []
            if pts_node:
                for xy in children(pts_node, "xy"):
                    pts.append((to_float(xy[1]), to_float(xy[2])))
        else:
            pts = []
        return [p for p in pts if p is not None]

    # -- ratsnest ---------------------------------------------------------------

    def _track_links(self) -> list[dict]:
        """Copper items that join points: segments, arcs, vias."""
        links = []
        for kind in ("segment", "arc"):
            for t in children(self.root, kind):
                start, end = child(t, "start"), child(t, "end")
                if start and end:
                    links.append(
                        {
                            "net": self._net_name(child(t, "net")),
                            "points": [
                                (to_float(start[1]), to_float(start[2])),
                                (to_float(end[1]), to_float(end[2])),
                            ],
                        }
                    )
        for v in children(self.root, "via"):
            at = child(v, "at")
            if at:
                links.append(
                    {
                        "net": self._net_name(child(v, "net")),
                        "points": [(to_float(at[1]), to_float(at[2]))],
                    }
                )
        return links

    def ratsnest(self) -> dict:
        """Unrouted net connections (airwires) from pad connectivity.

        Pads of one net already joined by tracks/vias are merged into one
        cluster (zone fills are NOT considered); the airwires are the
        minimum-spanning-tree edges between the remaining clusters.
        """
        pads = [p for p in self.pads() if p["net"]]
        by_net: dict[str, list[dict]] = {}
        for p in pads:
            by_net.setdefault(p["net"], []).append(p)

        links_by_net: dict[str, list[dict]] = {}
        for link in self._track_links():
            links_by_net.setdefault(link["net"], []).append(link)

        airwires = []
        total = 0.0
        for net, net_pads in sorted(by_net.items()):
            if len(net_pads) < 2:
                continue
            clusters = self._net_clusters(net_pads, links_by_net.get(net, []))
            airwires.extend(self._mst_airwires(net, clusters))
        for aw in airwires:
            total += aw["length_mm"]
        return {
            "airwire_count": len(airwires),
            "total_length_mm": round(total, 3),
            "airwires": sorted(airwires, key=lambda a: -a["length_mm"]),
            "note": "zone fills are ignored; pads connected only through a filled zone still show as airwires",
        }

    @staticmethod
    def _net_clusters(net_pads: list[dict], links: list[dict]) -> list[list[dict]]:
        """Group a net's pads into already-connected clusters via tracks."""
        parent = list(range(len(net_pads) + len(links)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a, b):
            parent[find(a)] = find(b)

        # link <-> link: shared endpoint (exact coordinates, tiny tolerance)
        point_owner: dict[tuple[float, float], int] = {}
        for li, link in enumerate(links):
            for pt in link["points"]:
                key = (round(pt[0], 3), round(pt[1], 3))
                if key in point_owner:
                    union(len(net_pads) + li, point_owner[key])
                else:
                    point_owner[key] = len(net_pads) + li
        # pad <-> link: endpoint within the pad's bounding circle
        for pi, pad in enumerate(net_pads):
            reach = max(pad["size_mm"]) / 2 + 0.01
            for li, link in enumerate(links):
                if any(math.dist((pad["x_mm"], pad["y_mm"]), pt) <= reach for pt in link["points"]):
                    union(pi, len(net_pads) + li)

        groups: dict[int, list[dict]] = {}
        for pi, pad in enumerate(net_pads):
            groups.setdefault(find(pi), []).append(pad)
        return list(groups.values())

    @staticmethod
    def _mst_airwires(net: str, clusters: list[list[dict]]) -> list[dict]:
        """Prim's MST over clusters; edge = closest pad pair between clusters."""
        if len(clusters) < 2:
            return []

        def closest(ca, cb):
            best = None
            for a in ca:
                for b in cb:
                    d = math.dist((a["x_mm"], a["y_mm"]), (b["x_mm"], b["y_mm"]))
                    if best is None or d < best[0]:
                        best = (d, a, b)
            return best

        in_tree = {0}
        edges = []
        while len(in_tree) < len(clusters):
            best = None
            for i in in_tree:
                for j in range(len(clusters)):
                    if j in in_tree:
                        continue
                    cand = closest(clusters[i], clusters[j])
                    if best is None or cand[0] < best[0][0]:
                        best = (cand, j)
            (d, a, b), j = best
            in_tree.add(j)
            edges.append(
                {
                    "net": net,
                    "from": f"{a['reference']}.{a['pad']}",
                    "to": f"{b['reference']}.{b['pad']}",
                    "from_pos_mm": [a["x_mm"], a["y_mm"]],
                    "to_pos_mm": [b["x_mm"], b["y_mm"]],
                    "length_mm": round(d, 3),
                }
            )
        return edges

    # -- editing --------------------------------------------------------------

    def move_footprint(self, ref: str, x: float, y: float, rotation: float | None = None) -> dict:
        """Move (and optionally rotate) one footprint. Does not save."""
        fp = self.find_footprint(ref)
        ox, oy, orot = _get_at(fp)
        new_rot = orot if rotation is None else _norm_angle(rotation)
        _set_at(fp, x, y, new_rot)
        delta = _norm_angle(new_rot - orot)
        if delta:
            # Stored pad/text angles include the footprint angle (KiCad quirk):
            # shift them by the delta; relative x/y offsets stay untouched.
            for kind in ("pad", "property", "fp_text"):
                for item in children(fp, kind):
                    ix, iy, iang = _get_at(item)
                    _set_at(item, ix, iy, _norm_angle(iang + delta))
        return {
            "reference": ref,
            "from": {"x_mm": ox, "y_mm": oy, "rotation_deg": orot},
            "to": {"x_mm": x, "y_mm": y, "rotation_deg": new_rot},
        }

    def set_board_outline_rect(self, x: float, y: float, width: float, height: float) -> dict:
        """Replace everything on Edge.Cuts with one rectangle. Does not save."""
        removed = 0
        keep = []
        for item in self.root:
            if isinstance(item, list) and item and isinstance(item[0], Sym) and str(item[0]) in GR_SHAPES:
                layer = child(item, "layer")
                if layer is not None and str(layer[1]) == "Edge.Cuts":
                    removed += 1
                    continue
            keep.append(item)
        self.root[:] = keep
        fill_token = "no" if self.version >= 20250000 else "none"
        rect = [
            Sym("gr_rect"),
            [Sym("start"), num(round(x, 6)), num(round(y, 6))],
            [Sym("end"), num(round(x + width, 6)), num(round(y + height, 6))],
            [Sym("stroke"), [Sym("width"), num(0.1)], [Sym("type"), Sym("solid")]],
            [Sym("fill"), Sym(fill_token)],
            [Sym("layer"), "Edge.Cuts"],
            _new_uuid_node(),
        ]
        self.root.append(rect)
        return {
            "removed_edge_cuts_items": removed,
            "outline": {"x_mm": x, "y_mm": y, "width_mm": width, "height_mm": height},
        }

    # -- info ------------------------------------------------------------------

    def board_info(self) -> dict:
        general = child(self.root, "general")
        thickness = child(general, "thickness") if general else None
        layers_node = child(self.root, "layers")
        layers = []
        if layers_node:
            for entry in layers_node[1:]:
                if isinstance(entry, list) and len(entry) >= 3:
                    layers.append({"name": str(entry[1]), "type": str(entry[2])})
        gen = child(self.root, "generator")
        gen_ver = child(self.root, "generator_version")
        bbox = self._edge_cuts_bbox()
        info = {
            "path": self.path,
            "file_format_version": self.version,
            "generator": f"{gen[1] if gen else '?'} {gen_ver[1] if gen_ver else ''}".strip(),
            "thickness_mm": to_float(thickness[1]) if thickness else None,
            "copper_layers": [l["name"] for l in layers if l["type"] == "signal"],
            "layers": layers,
            "footprint_count": len(self.footprint_nodes()),
            "net_count": len(self.list_nets()),
            "track_segment_count": len(children(self.root, "segment")) + len(children(self.root, "arc")),
            "via_count": len(children(self.root, "via")),
            "zone_count": len(children(self.root, "zone")),
        }
        if bbox:
            info["board_bbox_mm"] = {
                "min_x": round(bbox[0], 3),
                "min_y": round(bbox[1], 3),
                "max_x": round(bbox[2], 3),
                "max_y": round(bbox[3], 3),
                "width": round(bbox[2] - bbox[0], 3),
                "height": round(bbox[3] - bbox[1], 3),
            }
        else:
            info["board_bbox_mm"] = None
            info["warning"] = "no board outline found on Edge.Cuts"
        return info

    def _edge_cuts_bbox(self):
        box = [math.inf, math.inf, -math.inf, -math.inf]
        found = False
        for kind in GR_SHAPES:
            for shape in children(self.root, kind):
                layer = child(shape, "layer")
                if layer is None or str(layer[1]) != "Edge.Cuts":
                    continue
                for px, py in self._shape_points(shape, kind.replace("gr_", "")):
                    found = True
                    box[0] = min(box[0], px)
                    box[1] = min(box[1], py)
                    box[2] = max(box[2], px)
                    box[3] = max(box[3], py)
        return box if found else None

    def design_rules(self) -> dict:
        out: dict = {"source": [], "board_setup": {}, "rules": {}, "net_classes": []}
        setup = child(self.root, "setup")
        if setup is not None:
            out["source"].append(os.path.basename(self.path) + " (setup)")
            for entry in setup[1:]:
                if isinstance(entry, list) and len(entry) == 2 and not isinstance(entry[1], list):
                    try:
                        out["board_setup"][str(entry[0])] = to_float(entry[1])
                    except ValueError:
                        out["board_setup"][str(entry[0])] = str(entry[1])
        pro_path = self.path[: -len(".kicad_pcb")] + ".kicad_pro"
        if os.path.isfile(pro_path):
            out["source"].append(os.path.basename(pro_path))
            with open(pro_path, encoding="utf-8") as f:
                pro = json.load(f)
            out["rules"] = pro.get("board", {}).get("design_settings", {}).get("rules", {})
            net_settings = pro.get("net_settings", {})
            for cls in net_settings.get("classes", []):
                out["net_classes"].append(
                    {
                        k: cls.get(k)
                        for k in ("name", "clearance", "track_width", "via_diameter", "via_drill")
                    }
                )
            patterns = net_settings.get("netclass_patterns") or []
            if patterns:
                out["netclass_patterns"] = patterns
        else:
            out["note"] = (
                "no .kicad_pro project file next to the board; net-class rules unavailable"
            )
        return out

    # -- collision detection --------------------------------------------------

    def _courtyard_polys(self) -> list[dict]:
        """Per footprint, the convex courtyard polygon in board mm, per side.

        Returns ``{"reference": ref, "front": [(x,y)...], "back": [...]}`` with
        a side key present only when that side has a courtyard. Front and back
        are kept apart so parts on opposite sides of the board don't read as
        colliding (matching KiCad's per-side courtyard DRC).
        """
        out = []
        for fp in self.footprint_nodes():
            fx, fy, frot = _get_at(fp)
            sides: dict[str, list] = {}
            for kind in FP_SHAPES:
                for shape in children(fp, kind):
                    layer = child(shape, "layer")
                    lname = str(layer[1]) if layer else ""
                    if not lname.endswith(".CrtYd"):
                        continue
                    side = "back" if lname.startswith("B.") else "front"
                    for px, py in _full_shape_points(shape, kind.replace("fp_", "")):
                        dx, dy = _rot(px, py, frot)
                        sides.setdefault(side, []).append((fx + dx, fy + dy))
            entry = {"reference": self.fp_ref(fp)}
            for side, pts in sides.items():
                hull = _convex_hull(pts)
                if len(hull) >= 3:
                    entry[side] = hull
            out.append(entry)
        return out

    def collisions(self, references: list[str] | None = None,
                   clearance_mm: float = 0.0) -> list[dict]:
        """Footprint pairs whose courtyards overlap or sit closer than clearance.

        With ``clearance_mm`` 0, only true courtyard overlaps (a DRC error) are
        returned. A positive clearance also flags pairs merely closer than that
        gap. If ``references`` is given, only pairs touching one of those
        footprints are reported (use it to check the parts you just moved).
        """
        polys = self._courtyard_polys()
        wanted = set(references) if references else None
        pairs = []
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                a, b = polys[i], polys[j]
                if wanted is not None and a["reference"] not in wanted \
                        and b["reference"] not in wanted:
                    continue
                for side in ("front", "back"):
                    if side in a and side in b:
                        sep = _poly_separation(a[side], b[side])
                        if sep < clearance_mm:
                            pairs.append({
                                "a": a["reference"],
                                "b": b["reference"],
                                "side": side,
                                "gap_mm": round(sep, 4),
                                "overlap": sep < 0,
                            })
        pairs.sort(key=lambda p: p["gap_mm"])
        return pairs

    # -- board outline & zone geometry (for on-board / keep-out placement) ----

    def _edge_cuts_loops(self) -> list[list[tuple[float, float]]]:
        """Closed polygons reconstructed from the Edge.Cuts graphics.

        Rectangles and polys become loops directly; loose line/arc segments are
        stitched together by matching endpoints. Arcs are sampled to a few
        points. Returns every closed loop found (outer boundary plus any
        cut-outs); empty if the outline can't be reconstructed.
        """
        loops: list[list[tuple[float, float]]] = []
        open_segs: list[tuple] = []
        for kind in GR_SHAPES:
            for shape in children(self.root, kind):
                layer = child(shape, "layer")
                if layer is None or str(layer[1]) != "Edge.Cuts":
                    continue
                k = kind.replace("gr_", "")
                if k == "rect":
                    pts = _full_shape_points(shape, "rect")
                    if len(pts) == 4:
                        loops.append(pts)
                elif k == "poly":
                    pts = _full_shape_points(shape, "poly")
                    if len(pts) >= 3:
                        loops.append(pts)
                elif k == "circle":
                    pts = _full_shape_points(shape, "circle")  # bbox square
                    if len(pts) == 4:
                        loops.append(pts)
                elif k in ("line", "arc"):
                    pts = _full_shape_points(shape, k)
                    if len(pts) >= 2:
                        open_segs.append((pts[0], pts[-1], pts))
        loops.extend(_stitch_loops(open_segs))
        return [lp for lp in loops if len(lp) >= 3]

    def _zone_polygons(self, keepout_only: bool = False) -> list[dict]:
        """Outline polygon of each zone on the board (board mm)."""
        out = []
        for z in children(self.root, "zone"):
            poly_node = child(z, "polygon")
            pts_node = child(poly_node, "pts") if poly_node else None
            if not pts_node:
                continue
            pts = [(to_float(xy[1]), to_float(xy[2])) for xy in children(pts_node, "xy")]
            if len(pts) < 3:
                continue
            is_keepout = child(z, "keepout") is not None
            if keepout_only and not is_keepout:
                continue
            name = child(z, "name")
            out.append({
                "name": str(name[1]) if name and len(name) > 1 else "",
                "keepout": is_keepout,
                "polygon": pts,
            })
        return out

    def _placement_region(self):
        """(outer_outline_or_None, [blocker_polygons]) for on-board placement.

        The outer outline is the largest-area Edge.Cuts loop; smaller loops
        inside it are treated as cut-outs (blockers), as are keep-out zones.
        """
        loops = self._edge_cuts_loops()
        outer = max(loops, key=_poly_area) if loops else None
        blockers = []
        for lp in loops:
            if lp is not outer and outer is not None and _poly_inside(lp, outer):
                blockers.append(lp)
        for z in self._zone_polygons(keepout_only=True):
            blockers.append(z["polygon"])
        return outer, blockers

    # -- targeted queries -----------------------------------------------------

    def pad_position(self, reference: str, pad_number) -> dict:
        """Absolute position and net of a single pad (e.g. U1, '28')."""
        pad_number = str(pad_number)
        for p in self.pads():
            if p["reference"] == reference and p["pad"] == pad_number:
                return {k: p[k] for k in ("reference", "pad", "net", "x_mm",
                                          "y_mm", "angle_deg", "size_mm", "layers")}
        raise KeyError(f"no pad {reference}.{pad_number} in {os.path.basename(self.path)}")

    def net_endpoints(self, net: str) -> dict:
        """Every pad on a net with positions, plus how many track clusters exist."""
        net_pads = [p for p in self.pads() if p["net"] == net]
        if not net_pads:
            raise KeyError(f"no pads on net {net!r}")
        links = [l for l in self._track_links() if l["net"] == net]
        clusters = self._net_clusters(net_pads, links)
        return {
            "net": net,
            "pad_count": len(net_pads),
            "cluster_count": len(clusters),
            "pads": [
                {
                    "pad": f"{p['reference']}.{p['pad']}",
                    "x_mm": p["x_mm"],
                    "y_mm": p["y_mm"],
                    "layers": p["layers"],
                }
                for p in net_pads
            ],
        }

    def measure_placement_quality(self) -> dict:
        """Placement-quality components (not a single blended score).

        Returns the ratsnest length, airwire count, courtyard collision count,
        and area utilisation so the caller can see *which* dimension changed
        between passes. DRC is left to ``run_drc`` (it shells out to kicad-cli).
        """
        rn = self.ratsnest()
        cols = self.collisions()
        polys = self._courtyard_polys()
        used = sum(_poly_area(e["front"]) for e in polys if "front" in e)
        bbox = self._edge_cuts_bbox()
        board_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) if bbox else None
        return {
            "ratsnest_total_mm": rn["total_length_mm"],
            "airwire_count": rn["airwire_count"],
            "courtyard_collision_count": len(cols),
            "collisions": cols,
            "courtyard_area_mm2": round(used, 2),
            "board_area_mm2": round(board_area, 2) if board_area else None,
            "area_utilization": round(used / board_area, 4) if board_area else None,
            "note": "components, not a blended score; lower ratsnest and zero "
                    "collisions are better",
        }

    def nearest_free_position(self, reference: str, anchor_reference: str,
                              anchor_pad, min_clearance_mm: float = 0.2,
                              search_step_mm: float = 0.25,
                              angle_step_deg: float = 15.0,
                              max_radius_mm: float = 40.0,
                              on_board: bool = True,
                              avoid_rule_areas: bool = True) -> dict:
        """Closest position to an anchor pad where *reference*'s courtyard is clear.

        Searches outward in rings from the anchor pad and returns the first
        footprint origin (current rotation kept) at which this footprint's
        courtyard clears every other footprint by ``min_clearance_mm``. With
        ``on_board`` the courtyard must also sit wholly inside the board outline;
        with ``avoid_rule_areas`` it must not enter any keep-out zone or board
        cut-out. Does not move anything — returns the suggested (x_mm, y_mm).
        """
        fp = self.find_footprint(reference)
        anchor = self.pad_position(anchor_reference, anchor_pad)
        ax, ay = anchor["x_mm"], anchor["y_mm"]
        polys = self._courtyard_polys()
        mine = next((e for e in polys if e["reference"] == reference), None)
        others = [e for e in polys if e["reference"] != reference and "front" in e]
        outer, blockers = self._placement_region()
        if not on_board:
            outer = None
        if not avoid_rule_areas:
            blockers = []
        fx, fy, _ = _get_at(fp)
        if mine is None or "front" not in mine:
            return {
                "reference": reference, "x_mm": round(ax, 4), "y_mm": round(ay, 4),
                "min_gap_mm": None,
                "note": "footprint has no front courtyard; returning the anchor position",
            }
        cur = mine["front"]
        cx = sum(p[0] for p in cur) / len(cur)
        cy = sum(p[1] for p in cur) / len(cur)
        off_x, off_y = cx - fx, cy - fy

        def poly_at(center_x, center_y):
            ddx, ddy = center_x - cx, center_y - cy
            return [(px + ddx, py + ddy) for px, py in cur]

        def min_gap(cand):
            return min((_poly_separation(cand, e["front"]) for e in others),
                       default=math.inf)

        def allowed(cand):
            if outer is not None and not _poly_inside(cand, outer):
                return False
            return not any(_polys_intersect(cand, blk) for blk in blockers)

        r = 0.0
        while r <= max_radius_mm:
            if r == 0.0:
                candidates = [(ax, ay)]
            else:
                n = max(8, int(round(360.0 / angle_step_deg)))
                candidates = [
                    (ax + r * math.cos(math.radians(k * 360.0 / n)),
                     ay + r * math.sin(math.radians(k * 360.0 / n)))
                    for k in range(n)
                ]
            for center_x, center_y in candidates:
                cand = poly_at(center_x, center_y)
                gap = min_gap(cand)
                if gap >= min_clearance_mm and allowed(cand):
                    return {
                        "reference": reference,
                        "x_mm": round(center_x - off_x, 4),
                        "y_mm": round(center_y - off_y, 4),
                        "anchor": f"{anchor_reference}.{anchor_pad}",
                        "distance_from_anchor_mm": round(
                            math.hypot(center_x - ax, center_y - ay), 4),
                        "min_gap_mm": round(gap, 4) if gap != math.inf else None,
                    }
            r += search_step_mm
        return {
            "reference": reference, "x_mm": None, "y_mm": None, "min_gap_mm": None,
            "note": f"no clear on-board position with {min_clearance_mm}mm clearance "
                    f"found within {max_radius_mm}mm of the anchor",
        }

    def auto_place_decoupling(self, ic_reference: str, caps: list[str],
                              clearance_mm: float = 0.2,
                              strategy: str = "nearest_pin",
                              on_board: bool = True,
                              avoid_rule_areas: bool = True) -> dict:
        """Place each decoupling cap next to the IC pin it shares a net with.

        For every cap, finds an IC pad on a shared net (preferring a non-ground
        supply pin), then uses :meth:`nearest_free_position` to seat the cap's
        courtyard against the IC without overlapping anything already placed,
        staying inside the board outline and clear of keep-out zones. Caps are
        placed one at a time so they also clear each other. Does not save — the
        caller saves once.
        """
        self.find_footprint(ic_reference)  # validate
        before = self.ratsnest()["total_length_mm"]
        ic_pads = [p for p in self.pads() if p["reference"] == ic_reference]
        placed, unplaced = [], []
        for cap in caps:
            cap_pads = [p for p in self.pads() if p["reference"] == cap]
            if not cap_pads:
                unplaced.append({"cap": cap, "reason": "footprint not found"})
                continue
            cap_nets = {p["net"] for p in cap_pads if p["net"]}
            # prefer a supply (non-GND) pin so the cap body sits at the rail
            anchor = None
            for p in sorted(ic_pads, key=lambda q: "GND" in q["net"].upper()):
                if p["net"] in cap_nets:
                    anchor = p
                    break
            if anchor is None:
                unplaced.append({"cap": cap, "reason": "no net shared with the IC"})
                continue
            pos = self.nearest_free_position(
                cap, ic_reference, anchor["pad"], min_clearance_mm=clearance_mm,
                on_board=on_board, avoid_rule_areas=avoid_rule_areas)
            if pos.get("x_mm") is None:
                unplaced.append({"cap": cap, "reason": "no clear spot near the pin"})
                continue
            self.move_footprint(cap, pos["x_mm"], pos["y_mm"])
            placed.append({
                "cap": cap,
                "anchor_pad": f"{ic_reference}.{anchor['pad']}",
                "net": anchor["net"],
                "x_mm": pos["x_mm"],
                "y_mm": pos["y_mm"],
                "min_gap_mm": pos.get("min_gap_mm"),
            })
        after = self.ratsnest()["total_length_mm"]
        return {
            "ic": ic_reference,
            "strategy": strategy,
            "placed": placed,
            "unplaced": unplaced,
            "ratsnest_before_mm": before,
            "ratsnest_after_mm": after,
        }

    # -- zones / rule areas ---------------------------------------------------

    def _net_ref_node(self, net_name: str | None):
        """Build a ``(net ...)`` reference matching this file's net format.

        Reuses the exact atoms of an existing pad's net reference for the named
        net so KiCad ≤9 (numbered) and KiCad 10 (name-only) both round-trip.
        """
        if net_name:
            for fp in self.footprint_nodes():
                for pad in children(fp, "pad"):
                    nn = child(pad, "net")
                    if nn is not None and self._net_name(nn) == net_name:
                        return [Sym("net"), *nn[1:]]
            table = self._net_table()
            for num_s, nm in table.items():
                if nm == net_name:
                    return [Sym("net"), Sym(num_s), net_name]
            # name not found on any pad/table: emit name-only (KiCad 10 form)
            return [Sym("net"), net_name]
        # no net (keep-out / rule area)
        return [Sym("net"), Sym("0")] if self._net_table() else [Sym("net"), ""]

    def add_zone(self, polygon: list[tuple[float, float]], layers, net: str | None = None,
                 keepout: dict | None = None, name: str | None = None) -> dict:
        """Append a zone (filled copper pour or keep-out rule area). Does not save.

        ``polygon`` is the outline as [(x, y), ...] in board mm. ``layers`` is a
        comma string or list. For a keep-out, pass ``keepout`` mapping any of
        tracks/vias/pads/copperpour/footprints to True (= not allowed). For a
        copper pour, pass ``net``. Note: viaduct writes the zone outline and
        settings but does NOT compute the copper fill — KiCad fills it on open
        (or via ``run_drc(refill_zones=True)``).
        """
        poly = [(float(x), float(y)) for x, y in polygon]
        if len(poly) < 3:
            raise ValueError("a zone polygon needs at least 3 points")
        layer_list = layers if isinstance(layers, list) else \
            [l.strip() for l in layers.split(",") if l.strip()]
        if not layer_list:
            raise ValueError("at least one layer is required")

        zone = [Sym("zone"), self._net_ref_node(net), [Sym("net_name"), net or ""]]
        if len(layer_list) == 1:
            zone.append([Sym("layer"), layer_list[0]])
        else:
            zone.append([Sym("layers"), *layer_list])
        zone.append(_new_uuid_node())
        if name:
            zone.append([Sym("name"), name])
        zone.append([Sym("hatch"), Sym("edge"), num(0.5)])
        zone.append([Sym("connect_pads"), [Sym("clearance"), num(0 if keepout else 0.5)]])
        zone.append([Sym("min_thickness"), num(0.25)])
        if keepout:
            def ka(k):
                return Sym("not_allowed") if keepout.get(k) else Sym("allowed")
            zone.append([
                Sym("keepout"),
                [Sym("tracks"), ka("tracks")],
                [Sym("vias"), ka("vias")],
                [Sym("pads"), ka("pads")],
                [Sym("copperpour"), ka("copperpour")],
                [Sym("footprints"), ka("footprints")],
            ])
        zone.append([Sym("fill"),
                     [Sym("thermal_gap"), num(0.5)],
                     [Sym("thermal_bridge_width"), num(0.5)]])
        zone.append([Sym("polygon"),
                     [Sym("pts"), *[[Sym("xy"), num(x), num(y)] for x, y in poly]]])
        self.root.append(zone)
        return {
            "added": "keepout" if keepout else "filled_zone",
            "layers": layer_list,
            "net": net,
            "name": name,
            "points": len(poly),
            "note": None if keepout else "outline written; KiCad computes the "
                    "actual fill on open (or run_drc with refill_zones=True)",
        }

    def find_clear_region(self, min_width_mm: float, min_height_mm: float,
                          prefer_near_pad: str | None = None, layer: str = "F.Cu",
                          grid_step_mm: float = 0.5, clearance_mm: float = 0.0) -> dict:
        """Find an empty axis-aligned region of at least the given size.

        Scans the board on a grid for a min_width × min_height rectangle that is
        inside the outline, clear of every courtyard on ``layer`` (by
        ``clearance_mm``) and outside all keep-out zones / cut-outs. Returns the
        region closest to ``prefer_near_pad`` ('REF.PAD') or the board centre.
        """
        side = "back" if layer.startswith("B.") else "front"
        occupied = [e[side] for e in self._courtyard_polys() if side in e]
        outer, blockers = self._placement_region()
        bbox = self._edge_cuts_bbox()
        if not bbox:
            raise ValueError("no board outline found; cannot search for a clear region")
        if prefer_near_pad:
            if "." not in prefer_near_pad:
                raise ValueError("prefer_near_pad must be 'REF.PAD', e.g. 'U1.28'")
            pr, pd = prefer_near_pad.rsplit(".", 1)
            pp = self.pad_position(pr, pd)
            target = (pp["x_mm"], pp["y_mm"])
        else:
            target = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

        best = None
        x = bbox[0]
        while x + min_width_mm <= bbox[2]:
            y = bbox[1]
            while y + min_height_mm <= bbox[3]:
                rect = [(x, y), (x + min_width_mm, y),
                        (x + min_width_mm, y + min_height_mm), (x, y + min_height_mm)]
                ok = outer is None or _poly_inside(rect, outer)
                if ok and any(_polys_intersect(rect, b) for b in blockers):
                    ok = False
                if ok and any(_poly_separation(rect, o) < clearance_mm for o in occupied):
                    ok = False
                if ok:
                    cxx, cyy = x + min_width_mm / 2, y + min_height_mm / 2
                    dist = math.hypot(cxx - target[0], cyy - target[1])
                    if best is None or dist < best[0]:
                        best = (dist, x, y)
                y += grid_step_mm
            x += grid_step_mm

        if best is None:
            return {"found": False, "min_width_mm": min_width_mm,
                    "min_height_mm": min_height_mm, "layer": layer,
                    "note": "no clear region of that size on this layer"}
        _, bx, by = best
        return {
            "found": True,
            "x_mm": round(bx, 4), "y_mm": round(by, 4),
            "width_mm": min_width_mm, "height_mm": min_height_mm,
            "center_mm": [round(bx + min_width_mm / 2, 4),
                          round(by + min_height_mm / 2, 4)],
            "layer": layer,
            "distance_from_target_mm": round(best[0], 4),
        }

    # -- routing (manual: you supply the path, run_drc verifies) --------------

    def add_track(self, net: str, layer: str, points: list, width_mm: float) -> dict:
        """Append copper track segments along a polyline. Does not save."""
        pts = [(float(x), float(y)) for x, y in points]
        if len(pts) < 2:
            raise ValueError("a track needs at least 2 points")
        net_node = self._net_ref_node(net)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            self.root.append([
                Sym("segment"),
                [Sym("start"), num(round(x1, 6)), num(round(y1, 6))],
                [Sym("end"), num(round(x2, 6)), num(round(y2, 6))],
                [Sym("width"), num(width_mm)],
                [Sym("layer"), layer],
                [Sym("net"), *net_node[1:]],
                _new_uuid_node(),
            ])
        length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        return {"net": net, "layer": layer, "segments_added": len(pts) - 1,
                "length_mm": round(length, 3), "width_mm": width_mm}

    def place_via(self, x: float, y: float, from_layer: str, to_layer: str,
                  net: str, size_mm: float = 0.6, drill_mm: float = 0.3) -> dict:
        """Append a via at (x, y) joining two layers on a net. Does not save."""
        net_node = self._net_ref_node(net)
        self.root.append([
            Sym("via"),
            [Sym("at"), num(round(x, 6)), num(round(y, 6))],
            [Sym("size"), num(size_mm)],
            [Sym("drill"), num(drill_mm)],
            [Sym("layers"), from_layer, to_layer],
            [Sym("net"), *net_node[1:]],
            _new_uuid_node(),
        ])
        return {"net": net, "at_mm": [x, y], "layers": [from_layer, to_layer],
                "size_mm": size_mm, "drill_mm": drill_mm}

    def delete_tracks(self, net: str | None = None, layer: str | None = None,
                      uuid: str | None = None) -> dict:
        """Remove tracks/vias matching a filter. Requires at least one filter."""
        if net is None and layer is None and uuid is None:
            raise ValueError("specify at least one of net/layer/uuid — refusing to "
                             "delete every track on the board")
        removed = 0
        keep = []
        for item in self.root:
            if isinstance(item, list) and item and isinstance(item[0], Sym) \
                    and str(item[0]) in ("segment", "arc", "via"):
                lyr = child(item, "layer")
                u = child(item, "uuid")
                match = True
                if net is not None and self._net_name(child(item, "net")) != net:
                    match = False
                if layer is not None and (lyr is None or str(lyr[1]) != layer):
                    match = False
                if uuid is not None and (u is None or str(u[1]) != uuid):
                    match = False
                if match:
                    removed += 1
                    continue
            keep.append(item)
        self.root[:] = keep
        return {"removed": removed,
                "filter": {"net": net, "layer": layer, "uuid": uuid}}

    def measure_track_length(self, net: str) -> dict:
        """Total routed copper length on a net (segments + arcs), and via count."""
        total = 0.0
        nseg = 0
        for s in children(self.root, "segment"):
            if self._net_name(child(s, "net")) != net:
                continue
            st, en = child(s, "start"), child(s, "end")
            total += math.dist((to_float(st[1]), to_float(st[2])),
                               (to_float(en[1]), to_float(en[2])))
            nseg += 1
        for a in children(self.root, "arc"):
            if self._net_name(child(a, "net")) != net:
                continue
            st, mid, en = child(a, "start"), child(a, "mid"), child(a, "end")
            if st and mid and en:
                total += _arc_length((to_float(st[1]), to_float(st[2])),
                                     (to_float(mid[1]), to_float(mid[2])),
                                     (to_float(en[1]), to_float(en[2])))
                nseg += 1
        nvia = sum(1 for v in children(self.root, "via")
                   if self._net_name(child(v, "net")) == net)
        return {"net": net, "routed_length_mm": round(total, 3),
                "segment_count": nseg, "via_count": nvia}

    def generate_spiral_coil(self, center: tuple, od_mm: float, id_mm: float,
                             turns: float, trace_width_mm: float, layer: str,
                             net: str, points_per_turn: int = 48) -> dict:
        """Lay an Archimedean spiral as track segments on a net. Does not save.

        Spirals from id_mm to od_mm over ``turns`` revolutions. Returns the
        start/end coordinates so the ends can be routed to the rest of the net.
        """
        cx, cy = float(center[0]), float(center[1])
        r0, r1 = id_mm / 2.0, od_mm / 2.0
        if r1 <= r0:
            raise ValueError("od_mm must be greater than id_mm")
        if turns <= 0:
            raise ValueError("turns must be positive")
        n = max(8, int(round(points_per_turn * turns)))
        pts = []
        for i in range(n + 1):
            frac = i / n
            ang = 2 * math.pi * turns * frac
            r = r0 + (r1 - r0) * frac
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        res = self.add_track(net, layer, pts, trace_width_mm)
        res.update({
            "turns": turns,
            "start_mm": [round(pts[0][0], 4), round(pts[0][1], 4)],
            "end_mm": [round(pts[-1][0], 4), round(pts[-1][1], 4)],
        })
        return res

    def apply_netclass(self, net: str, class_name: str) -> dict:
        """Assign a net to an existing netclass in the sibling .kicad_pro. Saves it."""
        pro_path = self.path[: -len(".kicad_pcb")] + ".kicad_pro"
        if not os.path.isfile(pro_path):
            raise FileNotFoundError(
                "no .kicad_pro project file next to the board; netclasses live there")
        with open(pro_path, encoding="utf-8") as f:
            pro = json.load(f)
        ns = pro.setdefault("net_settings", {})
        defined = {c.get("name") for c in ns.get("classes", [])}
        if class_name not in defined:
            raise ValueError(
                f"netclass {class_name!r} is not defined in the project; "
                f"have: {sorted(n for n in defined if n)}")
        patterns = ns.setdefault("netclass_patterns", [])
        patterns[:] = [p for p in patterns if p.get("pattern") != net]
        patterns.append({"netclass": class_name, "pattern": net})
        backup = guarded_write(pro_path, json.dumps(pro, indent=2))
        return {"net": net, "netclass": class_name,
                "project_file": pro_path, "backup": backup}
