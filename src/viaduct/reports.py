"""Parse kicad-cli ERC/DRC JSON reports into clean summaries.

Shapes verified against kicad-cli 10.0.3:

DRC:  {"violations": [...], "unconnected_items": [...], "schematic_parity": [...],
       "coordinate_units": "mm", "kicad_version": "10.0.3", "source": ...}
ERC:  {"sheets": [{"path": "/", "uuid_path": ..., "violations": [...]}], ...}

Each violation: {"type": "courtyards_overlap", "severity": "error",
                 "description": ..., "items": [{"description", "pos": {"x","y"},
                 "uuid"}]}
"""

from __future__ import annotations

from collections import Counter


def _clean_violation(v: dict, sheet: str | None = None) -> dict:
    out = {
        "type": v.get("type", ""),
        "severity": v.get("severity", ""),
        "description": v.get("description", ""),
        "items": [
            {
                "description": item.get("description", ""),
                "x_mm": item.get("pos", {}).get("x"),
                "y_mm": item.get("pos", {}).get("y"),
            }
            for item in v.get("items", [])
        ],
    }
    if sheet is not None:
        out["sheet"] = sheet
    return out


def summarize_drc(raw: dict) -> dict:
    violations = [_clean_violation(v) for v in raw.get("violations", [])]
    unconnected = [_clean_violation(v) for v in raw.get("unconnected_items", [])]
    parity = [_clean_violation(v) for v in raw.get("schematic_parity", [])]
    everything = violations + unconnected + parity
    return {
        "kicad_version": raw.get("kicad_version"),
        "units": raw.get("coordinate_units", "mm"),
        "violation_count": len(violations),
        "unconnected_count": len(unconnected),
        "schematic_parity_count": len(parity),
        "by_severity": dict(Counter(v["severity"] for v in everything)),
        "by_type": dict(Counter(v["type"] for v in everything)),
        "violations": violations,
        "unconnected_items": unconnected,
        "schematic_parity": parity,
    }


def summarize_erc(raw: dict) -> dict:
    violations = []
    for sheet in raw.get("sheets", []):
        for v in sheet.get("violations", []):
            violations.append(_clean_violation(v, sheet=sheet.get("path", "/")))
    return {
        "kicad_version": raw.get("kicad_version"),
        "units": raw.get("coordinate_units", "mm"),
        "violation_count": len(violations),
        "by_severity": dict(Counter(v["severity"] for v in violations)),
        "by_type": dict(Counter(v["type"] for v in violations)),
        "violations": violations,
    }
