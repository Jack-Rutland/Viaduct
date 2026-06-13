# viaduct

An MCP server that lets Claude do PCB work in [KiCad](https://kicad.org): inspect boards,
place footprints, measure ratsnest length, run ERC/DRC, render images for visual review,
and export fabrication outputs.

No KiCad Python bindings required — viaduct parses `.kicad_pcb` / `.kicad_sch` files with
its own s-expression parser and shells out to `kicad-cli` for checks, exports, and renders.
Verified against KiCad 10.0 file output (KiCad 8/9 net tables are also understood).

## Install

Requirements: Python 3.10+, KiCad 8 or newer (developed and tested against KiCad 10.0).

```sh
git clone <this-repo> viaduct && cd viaduct
pip install .
```

Register with Claude Code:

```sh
claude mcp add viaduct -- viaduct
```

`kicad-cli` is found automatically (on macOS inside
`/Applications/KiCad/KiCad.app/Contents/MacOS/`). If yours lives elsewhere:

```sh
claude mcp add viaduct -e VIADUCT_KICAD_CLI=/path/to/kicad-cli -- viaduct
```

Sanity check: ask Claude to run the `kicad_version` tool.

## Tools

| Tool | What it does |
|---|---|
| `kicad_version` | Locate kicad-cli and report its version (install sanity check) |
| `run_drc` | Design Rules Check via kicad-cli; clean summary with severity, description, position (mm) |
| `run_erc` | Electrical Rules Check on a schematic; same summary format |
| `board_info` | Layers, thickness, outline bounding box, element counts |
| `list_footprints` | Every footprint: reference, value, position, rotation, layer |
| `list_nets` | All net names |
| `connectivity` | Per footprint: each pad's net name and absolute position (mm) |
| `courtyards` | Per footprint: courtyard bounding box in board coordinates (mm) |
| `ratsnest` | Unrouted connections as airwires (from-pad, to-pad, length mm) — the placement metric |
| `design_rules` | Clearance / track width / via rules from board setup + `.kicad_pro` net classes |
| `check_collisions` | Footprint pairs whose courtyards overlap (or sit closer than a clearance), via SAT |
| `pad_position` | Absolute position, net, and size of a single pad (e.g. `U1`, `28`) |
| `net_endpoints` | Every pad on a net with positions + how many track clusters remain |
| `measure_placement_quality` | Ratsnest length, airwire/collision counts, area use — components, not a blended score |
| `footprint_info` | Dimensions, pad count, courtyard of a library footprint, *without* placing it |
| `move_footprint` | Move/rotate one footprint (pad angles handled), with backup; can reject collisions |
| `move_footprints` | Batch move — one backup, one save; reports collisions, optional reject-on-collision |
| `nearest_free_position` | Closest spot to a pin where a footprint's courtyard clears everything (no move) |
| `auto_place_decoupling` | Seat decoupling caps against the IC pin each one shares a net with |
| `set_board_outline_rect` | Replace the Edge.Cuts outline with a rectangle |
| `add_rule_area` | Add a keep-out rule area over a polygon |
| `add_filled_zone` | Add a copper-pour zone outline on a net (KiCad computes the fill on open) |
| `restore_backup` | Restore a file from the `.bak` written before the last edit |
| `backup_list` / `backup_restore_to` | List the rolling numbered backup history and restore any entry |
| `list_symbols` | Schematic symbols: reference, value, lib_id, footprint, position |
| `list_labels` | Local/global/hierarchical net labels |
| `set_symbol_property` | Set Value, Footprint, MPN, … on a symbol |
| `export_gerbers` | Fabrication Gerbers + drill files |
| `export_bom` | CSV bill of materials |
| `export_step` | 3D STEP model |
| `export_netlist` | Netlist (kicadsexpr, kicadxml, spice, …) |
| `render_board_svg` | Selected layers to a single SVG (board area only) |
| `render_board_png` | PNG render returned as an image, so the agent can *see* the board |

All coordinates are millimetres in KiCad's board frame (X right, **Y down**); rotations are
degrees counter-clockwise.

## The placement loop

viaduct is built around an iterate-until-clean placement workflow:

1. **Understand** — `board_info`, `design_rules`, `list_footprints`, `connectivity`,
   `ratsnest`: what's on the board, what connects to what, how bad is it now.
2. **Place** — `move_footprints` in batches (decoupling caps next to their IC pins,
   connectors on edges, group by function). `move_footprints` reports any courtyard
   collisions it creates and can `reject_on_collision`; `nearest_free_position` and
   `auto_place_decoupling` place parts against a pin without overlap in the first place.
3. **Look** — `render_board_png`: the model visually inspects its own placement.
4. **Measure** — `measure_placement_quality` / `ratsnest`: airwire length and collision
   count should drop. `check_collisions` lists any remaining courtyard overlaps directly.
5. **Verify** — `run_drc`: fix courtyard overlaps and clearance violations.
6. Repeat 2–5 until DRC is clean and the render looks tidy.

## Important: close the board in KiCad first

Editing tools check for KiCad's lockfile (`~<name>.kicad_pcb.lck`) and **refuse to edit a
file that is open in KiCad** — KiCad would silently overwrite the changes on its next save.
Close the board (or schematic) in KiCad before asking Claude to edit it, then reopen to
review. Every edit writes a `.bak` next to the file first; `restore_backup` undoes the
last edit. (If KiCad crashed and left a stale lockfile, delete it manually.)

Renders, exports, and all read-only tools are safe to use while KiCad is open.

## Example prompts

> Place all decoupling caps within 2mm of their IC pins on mainboard.kicad_pcb, then show me a render.

> Run DRC on the board and fix all courtyard overlaps by nudging the offending parts apart.

> What's the total airwire length on revB.kicad_pcb, and which three nets contribute the most?

> Set the footprint of every 100n cap in power.kicad_sch to Capacitor_SMD:C_0402_1005Metric and re-export the BOM.

## Layout session prompt

Copy-paste this to start a full placement session:

---
Lay out the board at <path/to/board.kicad_pcb>. Process:
1. Read board_info, design_rules, list_footprints, connectivity, and
   ratsnest to understand the design before moving anything.
2. Plan placement: group by function, decoupling caps within 2mm of the
   pins they serve, connectors on board edges, crystals close to their
   MCU with short paths, minimize total airwire length and crossings.
3. Apply with move_footprints in batches.
4. render_board_png and visually inspect; check ratsnest length again.
5. run_drc; fix every courtyard overlap and clearance violation.
6. Repeat 3-5 until DRC is clean and the render looks tidy, then report
   total airwire length before vs after and remaining concerns.
Do not route traces. Never edit while the board is open in KiCad.
---

## Development

```sh
python3 tests/test_viaduct.py
```

The test script verifies viaduct against the real KiCad installation: it round-trips a
KiCad-saved board through the parser and confirms `kicad-cli` still accepts it, checks
computed pad positions against KiCad's own IPC-D-356 export (including rotated
footprints), and exercises every tool. The fixture project in `tests/test_project/` was
saved by KiCad 10.0.3.

## License

MIT
