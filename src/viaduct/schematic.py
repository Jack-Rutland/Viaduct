"""Read and edit .kicad_sch files (symbols, labels, properties)."""

from __future__ import annotations

import os
import uuid as uuid_mod

from . import sexpr
from .safety import guarded_write
from .sexpr import Sym, child, children, is_node, num, to_float


class Schematic:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        if not self.path.endswith(".kicad_sch"):
            raise ValueError(f"not a .kicad_sch file: {path}")
        with open(self.path, encoding="utf-8") as f:
            self.root = sexpr.parse(f.read())
        if not is_node(self.root, "kicad_sch"):
            raise ValueError(f"{path} does not look like a KiCad schematic file")

    def save(self) -> str:
        return guarded_write(self.path, sexpr.dumps(self.root))

    # -- symbols ------------------------------------------------------------

    def symbol_nodes(self):
        """Placed symbol instances (top level, with a lib_id) — not lib_symbols."""
        return [s for s in children(self.root, "symbol") if child(s, "lib_id") is not None]

    @staticmethod
    def _property(sym, name: str) -> str:
        for p in children(sym, "property"):
            if len(p) >= 3 and p[1] == name:
                return str(p[2])
        return ""

    def list_symbols(self) -> list[dict]:
        out = []
        for s in self.symbol_nodes():
            at = child(s, "at")
            lib_id = child(s, "lib_id")
            dnp = child(s, "dnp")
            in_bom = child(s, "in_bom")
            props = {
                str(p[1]): str(p[2])
                for p in children(s, "property")
                if len(p) >= 3
            }
            out.append(
                {
                    "reference": props.get("Reference", ""),
                    "value": props.get("Value", ""),
                    "lib_id": str(lib_id[1]) if lib_id else "",
                    "footprint": props.get("Footprint", ""),
                    "x_mm": to_float(at[1]) if at else 0.0,
                    "y_mm": to_float(at[2]) if at else 0.0,
                    "rotation_deg": to_float(at[3]) if at and len(at) > 3 else 0.0,
                    "dnp": bool(dnp) and str(dnp[1]) == "yes",
                    "in_bom": (str(in_bom[1]) == "yes") if in_bom else True,
                    "properties": {
                        k: v
                        for k, v in props.items()
                        if k not in ("Reference", "Value", "Footprint")
                    },
                }
            )
        return sorted(out, key=lambda s: s["reference"])

    def list_labels(self) -> list[dict]:
        out = []
        for kind in ("label", "global_label", "hierarchical_label", "netclass_flag"):
            for lab in children(self.root, kind):
                at = child(lab, "at")
                shape = child(lab, "shape")
                out.append(
                    {
                        "kind": kind,
                        "text": str(lab[1]) if len(lab) > 1 and not isinstance(lab[1], list) else "",
                        "x_mm": to_float(at[1]) if at else 0.0,
                        "y_mm": to_float(at[2]) if at else 0.0,
                        "rotation_deg": to_float(at[3]) if at and len(at) > 3 else 0.0,
                        "shape": str(shape[1]) if shape else None,
                    }
                )
        return out

    def set_symbol_property(self, reference: str, name: str, value: str) -> dict:
        """Set (or add) a property on the symbol with the given reference.

        Does not save; call save() after. Returns old/new values.
        """
        target = None
        for s in self.symbol_nodes():
            if self._property(s, "Reference") == reference:
                target = s
                break
        if target is None:
            refs = sorted(self._property(s, "Reference") for s in self.symbol_nodes())
            raise KeyError(
                f"no symbol with reference {reference!r} in "
                f"{os.path.basename(self.path)} (have: {', '.join(refs)})"
            )
        for p in children(target, "property"):
            if len(p) >= 3 and p[1] == name:
                old = str(p[2])
                p[2] = str(value)
                return {"reference": reference, "property": name, "old": old, "new": value}
        # add a new (hidden) property anchored at the symbol position
        at = child(target, "at")
        x = at[1] if at else num(0)
        y = at[2] if at else num(0)
        new_prop = [
            Sym("property"),
            str(name),
            str(value),
            [Sym("at"), Sym(str(x)), Sym(str(y)), num(0)],
            [Sym("hide"), Sym("yes")],
            [Sym("effects"), [Sym("font"), [Sym("size"), num(1.27), num(1.27)]]],
        ]
        # insert after the last existing property to keep KiCad's ordering
        last = 0
        for i, c in enumerate(target):
            if is_node(c, "property"):
                last = i
        target.insert(last + 1, new_prop)
        return {"reference": reference, "property": name, "old": None, "new": value}

    @property
    def uuid(self) -> str:
        u = child(self.root, "uuid")
        return str(u[1]) if u else str(uuid_mod.uuid4())
