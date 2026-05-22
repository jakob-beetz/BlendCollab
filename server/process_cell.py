"""
process_cell.py — Headless Blender processing script
=====================================================
Run by the server after every student publish:

    blender --background --python process_cell.py -- \
        --blend   /path/to/cell_A1_3_v123.blend \
        --cell_id A1.3 \
        --origin_x 3.0 \
        --origin_y 9.0 \
        --parcel_size 3.0 \
        --out_assets /path/to/cell_A1_3_assets.blend \
        --out_glb    /path/to/cell_A1_3.glb

What it does:
  1. Opens the student's .blend in background mode
  2. Filters out boundary helper objects (tagged bc_boundary or bc_imported)
  3. Filters out any objects outside the cell's parcel bounds (safety net)
  4. Gathers remaining objects into a Collection named after the cell
  5. Marks the collection as a Blender Asset with metadata
  6. Saves the asset collection to out_assets (.blend)
  7. Exports all student objects to out_glb (.glb) via the glTF exporter
     with the origin offset applied so the GLB sits at world 0,0
     (the dashboard viewer places it at the correct position itself)
  8. Exits with code 0 on success, 1 on error (server checks this)

Requires: Blender 4.0+ (for Asset marking API and glTF exporter)
"""

import sys
import os
import argparse
import time as _time
from datetime import datetime, timezone


# ── Structured log helper ─────────────────────────────────────────────────────
# Writes timestamped lines to stdout with flush so the server sees them
# immediately without buffering.

def _log(step: str, msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] [process_cell/{step}] {msg}", flush=True)


def _hr(label: str = "") -> None:
    sep = "-" * 56
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}] [process_cell] {sep} {label}",
          flush=True)

# ── Parse args from everything after "--" on the command line ─────────────────
# Blender consumes its own args; everything after "--" is passed to the script.

def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    p = argparse.ArgumentParser(description="BlenderCollab cell processor")
    p.add_argument("--blend",       required=True,  help="Input .blend file")
    p.add_argument("--cell_id",     required=True,  help="Cell ID, e.g. A1.3")
    p.add_argument("--origin_x",    required=True,  type=float)
    p.add_argument("--origin_y",    required=True,  type=float)
    p.add_argument("--parcel_size", required=True,  type=float)
    p.add_argument("--out_assets",  required=True,  help="Output _assets.blend path")
    p.add_argument("--out_glb",     required=True,  help="Output .glb path")
    return p.parse_args(argv)


def main():
    import bpy
    import mathutils

    args = parse_args()
    t_total = _time.monotonic()

    # ── 0. Log startup: Blender version + all arguments ───────────────────────
    _hr("STARTUP")
    bpy_ver   = bpy.app.version_string
    bpy_hash  = getattr(bpy.app, "build_hash", b"?")
    if isinstance(bpy_hash, bytes):
        bpy_hash = bpy_hash.decode("utf-8", errors="replace")
    bpy_date  = getattr(bpy.app, "build_date", b"?")
    if isinstance(bpy_date, bytes):
        bpy_date = bpy_date.decode("utf-8", errors="replace")

    _log("init", f"Blender {bpy_ver}  hash={bpy_hash}  date={bpy_date}")
    _log("init", f"cell_id      = {args.cell_id}")
    _log("init", f"blend input  = {args.blend}")
    _log("init", f"out_assets   = {args.out_assets}")
    _log("init", f"out_glb      = {args.out_glb}")
    _log("init", f"origin       = ({args.origin_x}, {args.origin_y})")
    _log("init", f"parcel_size  = {args.parcel_size} m")
    try:
        in_size = os.path.getsize(args.blend)
        _log("init", f"input size   = {in_size:,} bytes ({in_size/1_048_576:.2f} MB)")
    except OSError as exc:
        _log("init", f"WARNING: cannot stat input: {exc}")

    # ── 1. Open the student's blend ───────────────────────────────────────────
    _hr("STEP 1 — open .blend")
    t1 = _time.monotonic()
    bpy.ops.wm.open_mainfile(filepath=args.blend)
    scene = bpy.context.scene
    all_objs = list(scene.objects)
    _log("open", f"Opened in {_time.monotonic()-t1:.2f}s")
    _log("open", f"Scene name         : {scene.name!r}")
    _log("open", f"Total objects      : {len(all_objs)}")
    _log("open", f"Collections        : {len(bpy.data.collections)}")
    _log("open", f"Meshes             : {len(bpy.data.meshes)}")
    _log("open", f"Materials          : {len(bpy.data.materials)}")
    _log("open", f"Images             : {len(bpy.data.images)}")
    _log("open", "Object inventory:")
    for obj in sorted(all_objs, key=lambda o: o.name):
        _log("open", (
            f"  {obj.name!r:40s}  type={obj.type:12s}  "
            f"loc=({obj.location.x:7.2f},{obj.location.y:7.2f},{obj.location.z:7.2f})  "
            f"bc_boundary={bool(obj.get('bc_boundary'))}  "
            f"bc_imported={bool(obj.get('bc_imported'))}"
        ))

    # ── 2. Collect student objects ────────────────────────────────────────────
    _hr("STEP 2 — filter student objects")
    t2 = _time.monotonic()
    # Exclude:
    #   - Objects tagged bc_boundary (wire parcel helpers)
    #   - Objects tagged bc_imported (downloaded peer cells)
    #   - Camera, Light objects that aren't part of the installation
    #   - Objects whose XY centroid falls outside the parcel (safety)

    ox, oy = args.origin_x, args.origin_y
    ps     = args.parcel_size

    def in_parcel(obj):
        loc = obj.location
        return (ox - 0.1 <= loc.x <= ox + ps + 0.1 and
                oy - 0.1 <= loc.y <= oy + ps + 0.1)

    student_objects = []
    for obj in scene.objects:
        if obj.get("bc_boundary"):
            _log("filter", f"  SKIP  {obj.name!r:40s} reason=bc_boundary")
            continue
        if obj.get("bc_imported"):
            _log("filter", f"  SKIP  {obj.name!r:40s} reason=bc_imported")
            continue
        if obj.type in {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}:
            _log("filter", f"  SKIP  {obj.name!r:40s} reason=type:{obj.type}")
            continue
        if not in_parcel(obj):
            _log("filter",
                 f"  SKIP  {obj.name!r:40s} reason=out-of-parcel  "
                 f"loc=({obj.location.x:.2f},{obj.location.y:.2f})  "
                 f"parcel=[{ox:.1f}..{ox+ps:.1f}, {oy:.1f}..{oy+ps:.1f}]")
            continue
        _log("filter", f"  KEEP  {obj.name!r:40s} type={obj.type}")
        student_objects.append(obj)

    _log("filter", f"Kept {len(student_objects)} student object(s)  "
         f"(filtered {len(all_objs)-len(student_objects)} of {len(all_objs)})  "
         f"elapsed={_time.monotonic()-t2:.2f}s")

    if not student_objects:
        _log("filter", "WARNING: No student objects — outputs will be empty")

    # ── 3. Build a clean collection ───────────────────────────────────────────
    _hr("STEP 3 — build collection")
    t3 = _time.monotonic()
    coll_name = f"Cell_{args.cell_id.replace('.', '_')}"

    # Remove any existing collection with this name
    if coll_name in bpy.data.collections:
        bpy.data.collections.remove(bpy.data.collections[coll_name])

    student_coll = bpy.data.collections.new(coll_name)
    scene.collection.children.link(student_coll)

    # Deselect all, then move student objects into the student collection
    bpy.ops.object.select_all(action='DESELECT')
    for obj in student_objects:
        # Link into student collection (may already be in other collections)
        if obj.name not in student_coll.objects:
            student_coll.objects.link(obj)

    _log("collection", f"Collection '{coll_name}' created with {len(student_coll.objects)} objects  "
         f"elapsed={_time.monotonic()-t3:.2f}s")

    # ── 4. Mark as Blender Asset ──────────────────────────────────────────────
    _hr("STEP 4 — mark as asset")
    t4 = _time.monotonic()
    student_coll.asset_mark()
    student_coll.asset_data.description = (
        f"BlenderCollab cell {args.cell_id} — "
        f"origin ({args.origin_x}, {args.origin_y}) parcel {args.parcel_size}m"
    )
    # Store origin as custom property on the asset so the addon can read it
    student_coll["bc_cell_id"]    = args.cell_id
    student_coll["bc_origin_x"]   = args.origin_x
    student_coll["bc_origin_y"]   = args.origin_y
    student_coll["bc_parcel_size"] = args.parcel_size

    # Tag objects inside the collection
    for obj in student_objects:
        obj["bc_cell"] = args.cell_id

    _log("asset", f"Marked '{coll_name}' as asset  description='{student_coll.asset_data.description}'  "
         f"custom_props=bc_cell_id,bc_origin_x,bc_origin_y,bc_parcel_size  "
         f"elapsed={_time.monotonic()-t4:.2f}s")

    # ── 5. Save asset .blend ──────────────────────────────────────────────────
    _hr("STEP 5 — save asset .blend")
    t5 = _time.monotonic()
    os.makedirs(os.path.dirname(args.out_assets), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=args.out_assets, copy=False)
    assets_size = os.path.getsize(args.out_assets)
    _log("save_blend", f"Saved: {args.out_assets}")
    _log("save_blend", f"Size : {assets_size:,} bytes ({assets_size/1_048_576:.2f} MB)  "
         f"elapsed={_time.monotonic()-t5:.2f}s")

    # ── 6. Export GLB ─────────────────────────────────────────────────────────
    _hr("STEP 6 — export GLB")
    t6 = _time.monotonic()

    # ── 6a. Pre-convert non-MESH types and modifier stacks to plain MESH ──────
    # export_apply=True (below) evaluates the depsgraph, but explicit conversion
    # is more reliable for:
    #   META   — multiple metaball objects merge into one evaluated mesh;
    #             the exporter may not resolve the combined result correctly.
    #   CURVE / SURFACE / FONT — bevel, extrude, taper, and fill geometry is
    #             only materialised after conversion to mesh.
    #   MESH with modifiers — Subdivision, Boolean, Array, Geometry Nodes, etc.
    # bpy.ops.object.convert(target='MESH') applies the full modifier stack and
    # type-converts in one step.  The asset .blend (step 5) has already been
    # saved at this point, so mutating the scene here is safe.
    _types_to_convert = {"META", "CURVE", "SURFACE", "FONT"}
    _to_convert = [
        obj for obj in student_objects
        if obj.type in _types_to_convert
        or (obj.type == "MESH" and obj.modifiers)
    ]
    if _to_convert:
        type_summary = ", ".join(sorted({o.type for o in _to_convert}))
        mod_count    = sum(len(o.modifiers) for o in _to_convert if o.type == "MESH")
        _log("glb", f"Pre-converting {len(_to_convert)} object(s) to plain MESH "
             f"(types: {type_summary}  total_modifiers: {mod_count})")
        for obj in _to_convert:
            _log("glb", f"  convert: {obj.name!r:40s}  type={obj.type}"
                 + (f"  modifiers=[{', '.join(m.name for m in obj.modifiers)}]"
                    if obj.modifiers else ""))
        bpy.ops.object.select_all(action='DESELECT')
        for obj in _to_convert:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = _to_convert[0]
        # Snapshot names before convert — references may be invalidated afterwards
        _names_before = [obj.name for obj in student_objects]
        bpy.ops.object.convert(target='MESH')
        # META conversion merges sibling objects and removes them from the scene.
        # Re-build student_objects by name lookup; invalidated refs crash on access.
        before = len(_names_before)
        student_objects = [scene.objects[n] for n in _names_before if n in scene.objects]
        if len(student_objects) != before:
            _log("glb", f"  {before - len(student_objects)} object(s) merged away "
                 f"during META conversion — {len(student_objects)} object(s) remain")
        _log("glb", "Pre-conversion complete")
    else:
        _log("glb", "No META/CURVE/FONT/modifier objects — pre-conversion skipped")

    # Select (refreshed) student objects for export
    bpy.ops.object.select_all(action='DESELECT')
    for obj in student_objects:
        obj.select_set(True)

    # Shift objects so the parcel origin is at world 0,0 for the GLB.
    # The dashboard viewer will translate them back to the correct position.
    offset = mathutils.Vector((-ox, -oy, 0.0))
    selected_top_level = [obj for obj in student_objects if obj.parent is None]
    _log("glb", f"Applying origin offset ({-ox:.2f}, {-oy:.2f}, 0) "
         f"to {len(selected_top_level)} top-level objects")
    for obj in student_objects:
        # Only move top-level objects (no parent), children move with parent
        if obj.parent is None:
            obj.location += offset

    os.makedirs(os.path.dirname(args.out_glb), exist_ok=True)

    # Only pass params stable across Blender 4.x and 5.x.
    # export_colors removed in 4.0; Y-up is now the default (no flag needed).
    _log("glb", "Calling export_scene.gltf ...")
    bpy.ops.export_scene.gltf(
        filepath=args.out_glb,
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_materials="EXPORT",
        export_cameras=False,
        export_lights=False,
    )
    glb_size = os.path.getsize(args.out_glb)
    _log("glb", f"Exported: {args.out_glb}")
    _log("glb", f"Size    : {glb_size:,} bytes ({glb_size/1_048_576:.2f} MB)  "
         f"elapsed={_time.monotonic()-t6:.2f}s")

    # Shift back (not strictly needed since we exit, but keeps the .blend clean)
    for obj in student_objects:
        if obj.parent is None:
            obj.location -= offset

    _hr("DONE")
    _log("done", f"Cell {args.cell_id} processed successfully  "
         f"total_elapsed={_time.monotonic()-t_total:.2f}s")
    _log("done", f"  assets.blend : {os.path.getsize(args.out_assets):,} bytes")
    _log("done", f"  .glb         : {os.path.getsize(args.out_glb):,} bytes")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"[{ts}] [process_cell/ERROR] {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
