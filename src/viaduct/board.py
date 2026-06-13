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
