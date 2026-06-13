# viaduct — guidance for Claude Code sessions

viaduct is an MCP server (Python, single dependency: `mcp`) exposing KiCad PCB tools.
It parses `.kicad_pcb`/`.kicad_sch` files with its own s-expression parser
(`src/viaduct/sexpr.py`) and shells out to `kicad-cli` for ERC/DRC, exports, and renders.
**Never** introduce kicad-python, SWIG pcbnew bindings, kiutils, or other KiCad-derived
code — the from-scratch parser is the point of this project.

## Using the server for layout work

The tools compose into one core loop (this is what the README's "Layout session prompt"
encodes — follow it when asked to lay out a board):

1. **Understand first**: `board_info` (outline, layers), `design_rules` (clearances),
   `list_footprints`, `connectivity` (pad → net + absolute mm), `ratsnest`
   (total airwire mm = the placement quality metric). Don't move anything before this.
2. **Plan**: group by function; decoupling caps within 2mm of the pins they serve;
   connectors on board edges; crystals close to the MCU; minimize airwire length and
   crossings. Use `courtyards` bounding boxes to compute non-overlapping positions —
   courtyard overlap is a DRC error.
3. **Apply**: `move_footprints` (batch) — one backup, one file write. Rotation is
   absolute degrees CCW; pad angle bookkeeping is handled internally.
4. **Inspect**: `render_board_png` returns the image — actually look at it.
   Re-run `ratsnest`; total length should drop.
5. **Verify**: `run_drc`; fix every courtyard overlap and clearance violation.
6. Repeat 3–5 until clean. Report airwire length before vs after.

Placement only — do not attempt to route traces by editing the file.

## Conventions and caveats

- **Coordinates**: mm, board frame, X right, **Y down**. A part "below" another has a
  *larger* Y. Rotations: degrees CCW as displayed in the PCB editor.
- **Editor-closed rule**: editing tools refuse when KiCad's lockfile
  (`~<name>.kicad_pcb.lck`) exists. Tell the user to close the file in KiCad rather than
  working around it; a stale lockfile after a crash may be deleted manually. Read-only
  tools, renders, and exports are safe with KiCad open.
- **Backups**: every edit writes `<file>.bak` first (only the last edit is kept).
  `restore_backup` restores it. Prefer one `move_footprints` batch over many
  `move_footprint` calls so the .bak spans the whole pass.
- **Ratsnest** merges pads already joined by tracks/vias (zone fills are ignored — pads
  connected only through a copper pour still show as airwires). Airwires are MST edges,
  matching DRC's unconnected count.
- **design_rules** needs the sibling `.kicad_pro` for net-class rules; board-file `setup`
  alone has only a few values.
- **Collisions** (`check_collisions`/`collisions`) run SAT on the *convex hull* of each
  courtyard, with front and back courtyards compared separately (opposite-side parts don't
  collide). `gap_mm` is signed: negative = overlapping by that much. `clearance_mm` 0 means
  true overlaps only (a DRC error); a positive value also flags parts merely too close. A
  non-convex courtyard is approximated by its hull, so it errs toward *over*-reporting.
- **Zones**: `add_filled_zone` writes only the zone outline + settings — viaduct does **not**
  compute the copper fill (that's KiCad's fill engine). KiCad fills on open, or pass
  `refill_zones=True` to `run_drc`. `add_rule_area` is a keep-out and needs no fill. Net
  references in a new zone are copied from an existing pad on that net so both the KiCad ≤9
  numbered and KiCad 10 name-only formats round-trip.
- **Net format**: KiCad 10 stores nets by name only, `(net "GND")`; KiCad ≤9 uses a
  numbered net table. `board.py` handles both — keep it that way.
- **Pad angle quirk** (verified): a pad's stored `(at x y angle)` angle is the SUM of pad
  + footprint rotation, while x/y stay in the unrotated footprint frame. Rotating a
  footprint rewrites pad/property/fp_text angles by the delta. Absolute pad position:
  `ax = fx + px·cos(t) + py·sin(t)`, `ay = fy − px·sin(t) + py·cos(t)`.

## Project layout

- `src/viaduct/sexpr.py` — parser/serializer. Quoted strings → `str`, bare tokens →
  `Sym(str)`; numbers stay as source text so untouched values round-trip exactly.
- `src/viaduct/board.py` — board model: footprints, pads, courtyards, ratsnest, moves,
  plus SAT courtyard-collision detection (`_poly_separation`/`collisions`),
  placement helpers (`nearest_free_position`, `auto_place_decoupling`,
  `measure_placement_quality`), and zone/rule-area insertion (`add_zone`).
- `src/viaduct/footprints.py` — read `.kicad_mod` library files for `footprint_info`
  (dimensions/courtyard *without* placing); honours `VIADUCT_FOOTPRINT_DIRS`.
- `src/viaduct/schematic.py` — symbols, labels, properties.
- `src/viaduct/safety.py` — lockfile check, `.bak` backups, guarded writes, plus a
  rolling numbered backup history (`.vbakNNNN`, `list_backups`/`restore_to`).
- `src/viaduct/kicad_cli.py` / `cli_ops.py` / `reports.py` — kicad-cli discovery,
  subprocess ops, ERC/DRC JSON summarisation (shapes verified against kicad-cli 10.0.3).
- `src/viaduct/server.py` — FastMCP tool definitions only; logic lives in the modules
  above so tests run without the `mcp` package.
- `tests/test_project/` — fixture saved by KiCad 10.0.3. If you regenerate it, run it
  through `kicad-cli pcb upgrade` so it stays a genuine KiCad-saved file.

## Testing

```sh
python3 tests/test_viaduct.py
```

Requires a real KiCad install (kicad-cli is found automatically; override with
`VIADUCT_KICAD_CLI`). The script copies the fixture to a temp dir, so it never dirties
the repo. Key invariants it enforces — keep these passing:

- `kicad-cli pcb drc` accepts re-serialized and edited boards (round-trip proxy).
- Pad absolute positions match KiCad's IPC-D-356 export, including rotated footprints
  and footprints rotated *by viaduct*.
- Ratsnest airwire count equals DRC's unconnected count.
- Locked files are refused; `restore_backup` round-trips.

After changing the serializer or move logic, always re-run the full script — "KiCad
accepts the output" is the only ground truth that matters here.
