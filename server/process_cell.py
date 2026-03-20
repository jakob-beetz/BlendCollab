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

    print(f"[process_cell] Processing cell {args.cell_id}")
    print(f"[process_cell] Input:  {args.blend}")
    print(f"[process_cell] Assets: {args.out_assets}")
    print(f"[process_cell] GLB:    {args.out_glb}")

    # ── 1. Open the student's blend ───────────────────────────────────────────
    bpy.ops.wm.open_mainfile(filepath=args.blend)

    scene = bpy.context.scene

    # ── 2. Collect student objects ────────────────────────────────────────────
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
            continue
        if obj.get("bc_imported"):
            continue
        if obj.type in {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}:
            continue
        if not in_parcel(obj):
            print(f"[process_cell] Skipping out-of-parcel object: {obj.name}")
            continue
        student_objects.append(obj)

    print(f"[process_cell] Found {len(student_objects)} student objects")

    if not student_objects:
        print("[process_cell] WARNING: No student objects found — writing empty outputs")

    # ── 3. Build a clean collection ───────────────────────────────────────────
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

    # ── 4. Mark as Blender Asset ──────────────────────────────────────────────
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

    # ── 5. Save asset .blend ──────────────────────────────────────────────────
    os.makedirs(os.path.dirname(args.out_assets), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=args.out_assets, copy=False)
    print(f"[process_cell] Saved asset blend: {args.out_assets}")

    # ── 6. Export GLB ─────────────────────────────────────────────────────────
    # Select only student objects for export
    bpy.ops.object.select_all(action='DESELECT')
    for obj in student_objects:
        obj.select_set(True)

    # Shift objects so the parcel origin is at world 0,0 for the GLB.
    # The dashboard viewer will translate them back to the correct position.
    offset = mathutils.Vector((-ox, -oy, 0.0))
    for obj in student_objects:
        # Only move top-level objects (no parent), children move with parent
        if obj.parent is None:
            obj.location += offset

    os.makedirs(os.path.dirname(args.out_glb), exist_ok=True)

    bpy.ops.export_scene.gltf(
        filepath=args.out_glb,
        export_format="GLB",
        use_selection=True,
        export_apply=True,           # apply modifiers
        export_materials="EXPORT",
        export_colors=True,
        export_cameras=False,
        export_lights=False,
        export_yup=True,             # Three.js uses Y-up
    )
    print(f"[process_cell] Exported GLB: {args.out_glb}")

    # Shift back (not strictly needed since we exit, but keeps the .blend clean)
    for obj in student_objects:
        if obj.parent is None:
            obj.location -= offset

    print(f"[process_cell] Done — cell {args.cell_id}")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[process_cell] ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
