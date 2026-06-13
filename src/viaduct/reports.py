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


def compact_drc(full: dict, detail: str = "summary", top_n: int = 10) -> dict:
    """Reshape a full DRC summary for a context-friendly response.

    detail='summary' (default): counts by severity/type plus the ``top_n``
    worst items (errors first), without the full lists. detail='full': the
    complete report unchanged. Any other value is treated as a violation type
    to filter on (e.g. 'courtyards_overlap'), returning just those items.
    """
    if detail == "full":
        return full
    all_items = (full["violations"] + full["unconnected_items"]
                 + full["schematic_parity"])
    if detail and detail != "summary":
        items = [v for v in all_items if v["type"] == detail]
        return {"detail": detail, "count": len(items), "violations": items}
    worst = sorted(all_items, key=lambda v: 0 if v["severity"] == "error" else 1)
    return {
        "kicad_version": full["kicad_version"],
        "units": full["units"],
        "violation_count": full["violation_count"],
        "unconnected_count": full["unconnected_count"],
        "schematic_parity_count": full["schematic_parity_count"],
        "by_severity": full["by_severity"],
        "by_type": full["by_type"],
        "worst": worst[:top_n],
        "note": f"showing {min(top_n, len(all_items))} of {len(all_items)} items; "
                "call run_drc with detail='full' or detail='<type>' for the rest",
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
