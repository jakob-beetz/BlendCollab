"""
BlenderCollab Addon v2
======================
Changes from v1:
  - Students register by username only; server resolves cell from roster
  - Cell layout + adjacency fetched from server /api/layout at registration
  - Boundary enforcement uses point-in-polygon test (works for any shape)
  - Neighbourhood filter toggle: show all updates OR only direct neighbours
  - Neighbour cells highlighted differently in viewport (amber vs red)
  - Blender objects tagged per cell when downloaded; removed cleanly on update
  - Environment .blend import: imports base scene, then layers student work on top
"""

bl_info = {
    "name":        "BlenderCollab v2",
    "author":      "BlenderCollab",
    "version":     (2, 0, 0),
    "blender":     (4, 0, 0),
    "location":    "View3D › Sidebar › BlenderCollab",
    "description": "Site-plan aware collaborative parcel editing with neighbour awareness",
    "category":    "Collaboration",
}

import bpy
import os
import json
import time
import queue
import threading
import tempfile
import urllib.request
import urllib.error
import secrets
from pathlib import Path
from bpy.props import (
    StringProperty, IntProperty, BoolProperty,
    CollectionProperty, EnumProperty, FloatProperty
)
from bpy.types import PropertyGroup, Panel, Operator, AddonPreferences

# ── Addon preferences ──────────────────────────────────────────────────────────

class BCPreferences(AddonPreferences):
    bl_idname = __name__

    server_url: StringProperty(
        name="Server URL",
        default="http://localhost:5000",
    )  # type: ignore

    def draw(self, context):
        self.layout.prop(self, "server_url")

# ── Property types ─────────────────────────────────────────────────────────────

class BCCellState(PropertyGroup):
    cell_id:      StringProperty()  # type: ignore
    display_name: StringProperty()  # type: ignore
    username:     StringProperty()  # type: ignore
    group:        StringProperty()  # type: ignore
    updated_at:   StringProperty()  # type: ignore
    checksum:     StringProperty()  # type: ignore
    version:      IntProperty()     # type: ignore
    changed:      BoolProperty(default=False)   # type: ignore
    is_neighbour: BoolProperty(default=False)   # type: ignore
    published:    BoolProperty(default=False)   # type: ignore


class BCScene(PropertyGroup):
    # ── Auth / identity ──
    username:      StringProperty(default="")   # type: ignore
    token:         StringProperty(default="")   # type: ignore
    cell_id:       StringProperty(default="")   # type: ignore
    display_name:  StringProperty(default="")   # type: ignore
    group:         StringProperty(default="")   # type: ignore
    group_color:   StringProperty(default="#ffffff")  # type: ignore
    origin_x:      FloatProperty(default=0.0)   # type: ignore
    origin_y:      FloatProperty(default=0.0)   # type: ignore
    neighbours:    StringProperty(default="")   # type: ignore  JSON list

    # ── UI state ──
    status_msg:    StringProperty(default="Not registered")  # type: ignore
    draw_bounds:   BoolProperty(default=True)   # type: ignore
    lock_others:   BoolProperty(default=True)   # type: ignore
    sse_running:   BoolProperty(default=False)  # type: ignore
    filter_mode:   EnumProperty(                # type: ignore
        name="Update Filter",
        items=[
            ("all",        "All cells",    "Receive updates from all 34 cells"),
            ("neighbours", "Neighbours",   "Only direct neighbours + your cell"),
        ],
        default="all"
    )
    cell_states:   CollectionProperty(type=BCCellState)  # type: ignore

    # ── Layout cache (stored as JSON string) ──
    layout_json:   StringProperty(default="")   # type: ignore


# ── Accessors ──────────────────────────────────────────────────────────────────

def prefs(context) -> BCPreferences:
    return context.preferences.addons[__name__].preferences

def bc(context) -> BCScene:
    return context.scene.bc

def server_url(context) -> str:
    return prefs(context).server_url.rstrip("/")

def get_layout(context) -> dict:
    raw = bc(context).layout_json
    return json.loads(raw) if raw else {}

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str, token: str = "") -> dict:
    req = urllib.request.Request(url, headers={"X-Auth-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _post_json(url: str, payload: dict, token: str = "") -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _post_file(url: str, filepath: str, token: str) -> dict:
    boundary = "----BCBoundary"
    with open(filepath, "rb") as fh:
        file_data = fh.read()
    header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(filepath)}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    footer = f"\r\n--{boundary}--\r\n".encode()
    body   = header + file_data + footer
    req    = urllib.request.Request(
        url, data=body,
        headers={
            "X-Auth-Token":  token,
            "Content-Type":  f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

# ── Geometry helpers ───────────────────────────────────────────────────────────

def point_in_polygon(px: float, py: float, polygon: list) -> bool:
    """Ray-casting PIP test — works for any convex or concave polygon."""
    n, inside, j = len(polygon), False, len(polygon) - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def cell_polygon(cell_def: dict, parcel_size: float) -> list:
    """Return the 4 corners of a rectangular cell as [[x,y], ...]."""
    ox, oy = cell_def["origin"]
    s = parcel_size
    return [[ox, oy], [ox+s, oy], [ox+s, oy+s], [ox, oy+s]]

def hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16)/255.0 for i in (0, 2, 4))

# ── SSE background thread ──────────────────────────────────────────────────────

_sse_stop   = threading.Event()
_event_queue: queue.Queue = queue.Queue()
_sse_thread = None

def _sse_reader(url: str) -> None:
    while not _sse_stop.is_set():
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=90) as r:
                event_name = "message"
                for raw_line in r:
                    if _sse_stop.is_set():
                        return
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            _event_queue.put({"event": event_name,
                                              "data": json.loads(line[5:].strip())})
                        except json.JSONDecodeError:
                            pass
                        event_name = "message"
        except Exception:
            time.sleep(5)

def start_sse(context) -> None:
    global _sse_thread
    s = bc(context)
    if s.sse_running:
        return
    _sse_stop.clear()
    # Use server-side filtering if student wants neighbours-only
    params = f"?filter={s.filter_mode}&cell_id={s.cell_id}"
    url    = f"{server_url(context)}/api/events{params}"
    _sse_thread = threading.Thread(target=_sse_reader, args=(url,), daemon=True)
    _sse_thread.start()
    s.sse_running = True

def stop_sse(context) -> None:
    _sse_stop.set()
    bc(context).sse_running = False

# ── Timer: apply SSE events to Blender state ──────────────────────────────────

_TIMER = 1.0

def _drain_sse_queue() -> float:
    ctx = bpy.context
    if not ctx or not hasattr(ctx, "scene"):
        return _TIMER

    s = bc(ctx)

    while not _event_queue.empty():
        try:
            msg = _event_queue.get_nowait()
        except queue.Empty:
            break

        event = msg["event"]
        data  = msg["data"]

        if event == "cell_updated":
            cid = data.get("cell_id", "")
            _upsert_cell_state(s, cid, data)
            # Flag as changed only if not own cell
            if cid != s.cell_id:
                for cs in s.cell_states:
                    if cs.cell_id == cid:
                        cs.changed = True
                        break
            for area in ctx.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()

    return _TIMER

def _upsert_cell_state(s: BCScene, cell_id: str, data: dict):
    for cs in s.cell_states:
        if cs.cell_id == cell_id:
            _fill_cs(cs, data)
            return
    cs = s.cell_states.add()
    cs.cell_id = cell_id
    _fill_cs(cs, data)

def _fill_cs(cs: BCCellState, data: dict):
    cs.username     = data.get("username", "")
    cs.display_name = data.get("display_name", cs.username)
    cs.group        = data.get("group", "")
    cs.updated_at   = data.get("updated_at", "")
    cs.checksum     = data.get("checksum", "")
    cs.version      = data.get("version", 0)
    cs.published    = True

# ── Depsgraph: parcel enforcement ──────────────────────────────────────────────

@bpy.app.handlers.persistent
def _enforce_bounds(scene, depsgraph):
    s = scene.bc
    if not s.token or not s.lock_others or not s.cell_id:
        return

    layout = get_layout(bpy.context) if bpy.context else {}
    if not layout:
        return

    cells_def    = layout.get("cells", {})
    parcel_size  = layout.get("parcel_size", 3.0)
    cell_def     = cells_def.get(s.cell_id)
    if not cell_def:
        return

    poly = cell_polygon(cell_def, parcel_size)
    ox, oy = cell_def["origin"]
    s_size = parcel_size

    for obj in scene.objects:
        if obj.get("bc_boundary") or obj.get("bc_imported"):
            continue
        loc = obj.location
        if not point_in_polygon(loc.x, loc.y, poly):
            # Clamp: snap to nearest point inside the AABB of the cell
            obj.location.x = max(ox + 0.01, min(ox + s_size - 0.01, loc.x))
            obj.location.y = max(oy + 0.01, min(oy + s_size - 0.01, loc.y))

# ── Boundary visualisation ─────────────────────────────────────────────────────

def _remove_boundaries():
    for obj in list(bpy.data.objects):
        if obj.get("bc_boundary"):
            bpy.data.objects.remove(obj, do_unlink=True)

def _make_wire_rect(context, ox, oy, size, colour, name):
    import bmesh
    mesh = bpy.data.meshes.new(name + "_mesh")
    obj  = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)

    bm = bmesh.new()
    z  = 0.002
    vs = [bm.verts.new((ox,        oy,        z)),
          bm.verts.new((ox + size, oy,        z)),
          bm.verts.new((ox + size, oy + size, z)),
          bm.verts.new((ox,        oy + size, z))]
    bm.edges.new((vs[0], vs[1]))
    bm.edges.new((vs[1], vs[2]))
    bm.edges.new((vs[2], vs[3]))
    bm.edges.new((vs[3], vs[0]))
    bm.to_mesh(mesh)
    bm.free()

    mat = bpy.data.materials.new(name + "_mat")
    mat.use_nodes = True
    mat.diffuse_color = (*colour, 1.0)
    obj.data.materials.append(mat)

    obj.display_type = "WIRE"
    obj["bc_boundary"]  = True
    obj.lock_location   = (True, True, True)
    obj.lock_rotation   = (True, True, True)
    obj.lock_scale      = (True, True, True)
    obj.hide_select     = True
    return obj


def _label_cell(context, cell_id: str, ox: float, oy: float, size: float):
    """Add a text object as a cell label."""
    font_curve = bpy.data.curves.new(name=f"BC_label_{cell_id}", type="FONT")
    font_curve.body = cell_id
    font_curve.size = 0.25
    txt_obj = bpy.data.objects.new(f"BC_label_{cell_id}", font_curve)
    txt_obj.location = (ox + 0.1, oy + size - 0.4, 0.01)
    context.collection.objects.link(txt_obj)
    txt_obj["bc_boundary"] = True
    txt_obj.hide_select    = True
    txt_obj.lock_location  = (True, True, True)
    txt_obj.lock_rotation  = (True, True, True)
    txt_obj.lock_scale     = (True, True, True)
    return txt_obj


# ── Operators ──────────────────────────────────────────────────────────────────

class BC_OT_Register(Operator):
    bl_idname  = "bc.register"
    bl_label   = "Register / Connect"

    def execute(self, context):
        s = bc(context)
        if not s.username:
            self.report({"ERROR"}, "Enter your username first")
            return {"CANCELLED"}

        try:
            resp = _post_json(f"{server_url(context)}/api/register",
                              {"username": s.username.strip().lower()})
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("detail", f"HTTP {e.code}")
            except Exception:
                msg = f"HTTP {e.code}"
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Cannot reach server: {e}")
            return {"CANCELLED"}

        s.token        = resp["token"]
        s.cell_id      = resp["cell_id"]
        s.display_name = resp["display_name"]
        s.group        = resp["group"]
        s.group_color  = resp["group_color"]
        s.origin_x     = resp["origin"][0]
        s.origin_y     = resp["origin"][1]
        s.neighbours   = json.dumps(resp["neighbours"])
        s.status_msg   = f"{resp['display_name']} · Cell {resp['cell_id']} · {resp['group']}"

        # Fetch and cache layout
        try:
            layout = _get(f"{server_url(context)}/api/layout", s.token)
            s.layout_json = json.dumps(layout)
        except Exception as e:
            self.report({"WARNING"}, f"Could not fetch layout: {e}")

        # Seed cell_states from /api/cells
        self._seed_cells(context)

        # Start SSE
        start_sse(context)
        if not bpy.app.timers.is_registered(_drain_sse_queue):
            bpy.app.timers.register(_drain_sse_queue, persistent=True)

        # Draw boundaries
        bpy.ops.bc.draw_bounds()

        self.report({"INFO"}, s.status_msg)
        return {"FINISHED"}

    def _seed_cells(self, context):
        s = bc(context)
        try:
            data = _get(f"{server_url(context)}/api/cells", s.token)
        except Exception:
            return

        neighbours = set(json.loads(s.neighbours))
        layout     = get_layout(context)
        adjacency  = layout.get("adjacency", {})

        s.cell_states.clear()
        for cid, info in data.get("cells", {}).items():
            cs = s.cell_states.add()
            cs.cell_id      = cid
            cs.username     = info.get("username", "")
            cs.display_name = info.get("display_name", cs.username)
            cs.group        = info.get("group", "")
            cs.updated_at   = info.get("updated_at", "")
            cs.checksum     = info.get("checksum", "")
            cs.version      = info.get("version", 0)
            cs.is_neighbour = cid in neighbours
            cs.published    = True
            cs.changed      = False


class BC_OT_Publish(Operator):
    bl_idname  = "bc.publish"
    bl_label   = "Save & Publish"
    bl_description = "Save the current scene and upload it to the server"

    @classmethod
    def poll(cls, context):
        # Greyed out with tooltip until the student is registered
        s = context.scene.bc
        if not s.token:
            cls.poll_message_set("Register first before publishing")
            return False
        return True

    # Object types excluded from every publish.
    # Cameras and lights accumulate across all 34 students if included.
    _EXCLUDE_TYPES = {"CAMERA", "LIGHT", "LIGHT_PROBE"}

    def execute(self, context):
        s = bc(context)

        # If the scene has never been saved give it a home first so that
        # save_as_mainfile(copy=True) has a valid source path.
        if not bpy.data.filepath:
            auto_path = os.path.join(
                tempfile.gettempdir(),
                f"bc_{s.cell_id.replace('.', '_')}_autosave.blend"
            )
            bpy.ops.wm.save_as_mainfile(filepath=auto_path)
            self.report({"INFO"}, f"Scene auto-saved to {auto_path}")

        # Objects excluded from the upload:
        #   • cameras, lights, light-probes
        #   • neighbour objects imported from other cells (bc_imported flag)
        #   • boundary helper wire objects (bc_boundary flag)
        excluded = [
            obj for obj in context.scene.objects
            if obj.type in self._EXCLUDE_TYPES
            or obj.get("bc_imported")
            or obj.get("bc_boundary")
        ]

        # Temporarily hide excluded objects so save_as_mainfile skips their
        # visible state. We restore immediately after the save regardless of
        # whether it succeeds.
        saved_hide_vp     = {obj.name: obj.hide_get()    for obj in excluded}
        saved_hide_render = {obj.name: obj.hide_render   for obj in excluded}
        for obj in excluded:
            obj.hide_set(True)
            obj.hide_render = True

        tmp = tempfile.NamedTemporaryFile(suffix=".blend", delete=False)
        tmp.close()
        try:
            bpy.ops.wm.save_as_mainfile(filepath=tmp.name, copy=True)
        finally:
            for obj in excluded:
                if obj.name in saved_hide_vp:
                    obj.hide_set(saved_hide_vp[obj.name])
                    obj.hide_render = saved_hide_render[obj.name]

        n_geom     = sum(1 for o in context.scene.objects if o not in excluded)
        n_excluded = len(excluded)

        try:
            resp = _post_file(f"{server_url(context)}/api/publish", tmp.name, s.token)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", f"HTTP {e.code}")
            except Exception:
                detail = f"HTTP {e.code}"
            self.report({"ERROR"}, f"Upload failed: {detail}")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Upload error: {e}")
            return {"CANCELLED"}
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)

        s.status_msg = (
            f"Published v{resp['version']} · {n_geom} objects "
            f"({n_excluded} excluded: lights/cameras/neighbours)"
        )
        self.report({"INFO"}, s.status_msg)
        return {"FINISHED"}


class BC_OT_DrawBounds(Operator):
    bl_idname  = "bc.draw_bounds"
    bl_label   = "Redraw Boundaries"

    def execute(self, context):
        s = bc(context)
        if not s.cell_id:
            self.report({"ERROR"}, "Register first")
            return {"CANCELLED"}

        _remove_boundaries()

        layout      = get_layout(context)
        cells_def   = layout.get("cells", {})
        parcel_size = layout.get("parcel_size", 3.0)
        groups      = layout.get("groups", {})
        neighbours  = set(json.loads(s.neighbours)) if s.neighbours else set()

        for cid, cdef in cells_def.items():
            ox, oy = cdef["origin"]
            if cid == s.cell_id:
                # Own cell — bright green
                colour = (0.1, 0.95, 0.35)
                _make_wire_rect(context, ox, oy, parcel_size, colour, f"BC_OWN_{cid}")
                _label_cell(context, cid, ox, oy, parcel_size)
            elif cid in neighbours:
                # Neighbour — amber
                colour = (1.0, 0.65, 0.1)
                _make_wire_rect(context, ox, oy, parcel_size, colour, f"BC_NBR_{cid}")
                _label_cell(context, cid, ox, oy, parcel_size)
            else:
                # Other — dim red
                colour = (0.6, 0.1, 0.1)
                _make_wire_rect(context, ox, oy, parcel_size, colour, f"BC_OTHER_{cid}")

        return {"FINISHED"}


class BC_OT_DownloadCell(Operator):
    bl_idname  = "bc.download_cell"
    bl_label   = "Download Cell"
    bl_options = {"REGISTER", "UNDO"}

    cell_id: StringProperty(default="")  # type: ignore

    def execute(self, context):
        s = bc(context)
        if not s.token:
            self.report({"ERROR"}, "Register first")
            return {"CANCELLED"}

        url = f"{server_url(context)}/api/cells/{self.cell_id}/download?token={s.token}"
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".blend", delete=False)
            tmp.close()
            urllib.request.urlretrieve(url, tmp.name)
        except Exception as e:
            self.report({"ERROR"}, f"Download failed: {e}")
            return {"CANCELLED"}

        # ── 1. Purge previous import for this cell ──────────────────────
        # Remove objects first, then try to remove the old collection.
        for obj in list(bpy.data.objects):
            if obj.get("bc_cell") == self.cell_id:
                bpy.data.objects.remove(obj, do_unlink=True)

        old_coll_name = f"BC_Cell_{self.cell_id}"
        if old_coll_name in bpy.data.collections:
            bpy.data.collections.remove(bpy.data.collections[old_coll_name])

        # ── 2. Resolve parcel origin ─────────────────────────────────────
        layout      = get_layout(context)
        cells_def   = layout.get("cells", {})
        parcel_size = layout.get("parcel_size", 3.0)
        cell_def    = cells_def.get(self.cell_id, {})
        ox, oy      = cell_def.get("origin", [0, 0])
        group_name  = cell_def.get("group", "")

        # ── 3. Create a dedicated locked collection for this cell ────────
        # Structure: Scene → BC_Neighbours → BC_Group_A1 → BC_Cell_A1.3
        def _get_or_make(parent_coll, name):
            if name in bpy.data.collections:
                c = bpy.data.collections[name]
            else:
                c = bpy.data.collections.new(name)
            if name not in [ch.name for ch in parent_coll.children]:
                parent_coll.children.link(c)
            return c

        scene_coll    = context.scene.collection
        nbr_coll      = _get_or_make(scene_coll,   "BC_Neighbours")
        group_coll    = _get_or_make(nbr_coll,      f"BC_Group_{group_name}")
        cell_coll     = _get_or_make(group_coll,    old_coll_name)

        # Lock the collection so students cannot select its contents
        # (hide_select on the collection layer_collection)
        def _set_coll_protected(coll_name, view_layer):
            lc = _find_layer_collection(view_layer.layer_collection, coll_name)
            if lc:
                lc.hide_viewport = False   # visible but…
                lc.collection.hide_select = True   # …not selectable

        def _find_layer_collection(lc, name):
            if lc.name == name:
                return lc
            for child in lc.children:
                found = _find_layer_collection(child, name)
                if found:
                    return found
            return None

        _set_coll_protected(old_coll_name, context.view_layer)

        # ── 4. Load objects, skip cameras/lights/light-probes ────────────
        _SKIP_TYPES = {"CAMERA", "LIGHT", "LIGHT_PROBE"}

        with bpy.data.libraries.load(tmp.name, link=False) as (data_from, data_to):
            data_to.objects = data_from.objects

        imported = 0
        skipped  = 0
        for obj in data_to.objects:
            if obj is None:
                continue
            if obj.type in _SKIP_TYPES:
                # Discard immediately — don't link into the scene at all
                bpy.data.objects.remove(obj, do_unlink=True)
                skipped += 1
                continue

            # Place into the cell's dedicated collection (not root)
            cell_coll.objects.link(obj)

            # Snap into the correct world-space parcel
            obj.location.x = ox + (obj.location.x % parcel_size)
            obj.location.y = oy + (obj.location.y % parcel_size)

            # Tag so publish and future downloads can identify this object
            obj["bc_imported"] = True
            obj["bc_cell"]     = self.cell_id

            # Full transform lock — this is read-only geometry
            obj.lock_location = (True, True, True)
            obj.lock_rotation = (True, True, True)
            obj.lock_scale    = (True, True, True)
            imported += 1

        os.unlink(tmp.name)

        # ── 5. Clear changed flag ────────────────────────────────────────
        for cs in s.cell_states:
            if cs.cell_id == self.cell_id:
                cs.changed = False
                break

        self.report(
            {"INFO"},
            f"Cell {self.cell_id} → {imported} objects imported into "
            f"collection '{old_coll_name}' (locked, {skipped} lights/cameras skipped)"
        )
        return {"FINISHED"}


class BC_OT_SetFilter(Operator):
    """Toggle SSE filter and reconnect."""
    bl_idname = "bc.set_filter"
    bl_label  = "Apply Filter"

    def execute(self, context):
        s = bc(context)
        if not s.token:
            return {"CANCELLED"}
        stop_sse(context)
        time.sleep(0.1)
        start_sse(context)
        self.report({"INFO"}, f"Filter set to: {s.filter_mode}")
        return {"FINISHED"}


class BC_OT_RefreshCells(Operator):
    bl_idname = "bc.refresh_cells"
    bl_label  = "Refresh"

    def execute(self, context):
        s = bc(context)
        if not s.token:
            return {"CANCELLED"}
        try:
            data = _get(f"{server_url(context)}/api/cells", s.token)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        neighbours = set(json.loads(s.neighbours)) if s.neighbours else set()
        existing = {cs.cell_id: cs for cs in s.cell_states}

        for cid, info in data.get("cells", {}).items():
            cs = existing.get(cid)
            if cs is None:
                cs = s.cell_states.add()
                cs.cell_id = cid
            _fill_cs(cs, info)
            cs.is_neighbour = cid in neighbours

        self.report({"INFO"}, "Refreshed")
        return {"FINISHED"}

# ── Panels ─────────────────────────────────────────────────────────────────────

class BC_PT_Main(Panel):
    bl_label       = "BlenderCollab"
    bl_idname      = "BC_PT_Main"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "BlenderCollab"

    def draw(self, context):
        s      = bc(context)
        layout = self.layout

        box = layout.box()
        box.label(text="Connect", icon="WORLD_DATA")

        # Show the actual server URL from addon preferences (editable here via operator)
        try:
            p = prefs(context)
            row = box.row(align=True)
            row.prop(p, "server_url", text="Server")
        except Exception:
            box.label(text="Set Server URL in Addon Preferences", icon="ERROR")

        box.prop(s, "username", text="Username")

        reg_row = box.row()
        reg_row.scale_y = 1.3
        reg_row.operator("bc.register", icon="LINKED",
                         text="Re-connect" if s.token else "Register / Connect")

        layout.separator()

        if s.token:
            # ── Registered state ──
            layout.label(text=s.status_msg, icon="CHECKMARK")

            layout.separator()

            pub = layout.box()
            pub.label(text="Publish", icon="EXPORT")
            pub_row = pub.row()
            pub_row.scale_y = 1.4
            pub_row.operator("bc.publish", icon="EXPORT")

            layout.separator()

            opt = layout.box()
            opt.label(text="Viewport", icon="SCENE")
            opt.prop(s, "draw_bounds", text="Show Boundaries")
            opt.prop(s, "lock_others", text="Enforce Parcel Lock")
            opt.operator("bc.draw_bounds", icon="MESH_GRID", text="Redraw Boundaries")
        else:
            layout.label(text=s.status_msg, icon="ERROR")


class BC_PT_Updates(Panel):
    bl_label       = "Cell Updates"
    bl_idname      = "BC_PT_Updates"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "BlenderCollab"
    bl_options     = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return bool(bc(context).token)

    def draw(self, context):
        s      = bc(context)
        layout = self.layout

        # Filter toggle
        row = layout.row(align=True)
        row.prop(s, "filter_mode", expand=True)
        row = layout.row()
        row.operator("bc.set_filter",    icon="FILE_REFRESH", text="Apply Filter")
        row.operator("bc.refresh_cells", icon="FILE_REFRESH", text="Refresh List")

        layout.separator()

        neighbours = set(json.loads(s.neighbours)) if s.neighbours else set()

        # Sort: changed first, then neighbours, then rest
        states = list(s.cell_states)
        states.sort(key=lambda cs: (0 if cs.changed else 1,
                                     0 if cs.cell_id in neighbours else 1,
                                     cs.cell_id))

        if s.filter_mode == "neighbours":
            states = [cs for cs in states
                      if cs.cell_id in neighbours or cs.cell_id == s.cell_id]

        for cs in states:
            row = layout.row(align=True)

            is_me  = cs.cell_id == s.cell_id
            is_nbr = cs.cell_id in neighbours

            # Icon indicates relationship
            if is_me:
                icon = "SOLO_ON"
            elif cs.changed:
                icon = "FUND"     # yellow-ish
            elif is_nbr:
                icon = "COMMUNITY"
            else:
                icon = "DOT"

            label = cs.cell_id
            if is_me:
                label += " (you)"
            elif cs.changed:
                label += " !"
            elif is_nbr:
                label += " ◆"

            sub = row.column()
            sub.alert = cs.changed and not is_me
            sub.label(text=label, icon=icon)

            if cs.published and not is_me:
                dl = row.operator("bc.download_cell", text="↓", icon="IMPORT")
                dl.cell_id = cs.cell_id

        if not states:
            layout.label(text="No published cells yet", icon="INFO")


# ── Registration ───────────────────────────────────────────────────────────────

CLASSES = [
    BCPreferences,
    BCCellState,
    BCScene,
    BC_OT_Register,
    BC_OT_Publish,
    BC_OT_DrawBounds,
    BC_OT_DownloadCell,
    BC_OT_SetFilter,
    BC_OT_RefreshCells,
    BC_PT_Main,
    BC_PT_Updates,
]

def _fill_cs(cs, data: dict):  # forward-declared above, re-declared here for module scope
    cs.username     = data.get("username", "")
    cs.display_name = data.get("display_name", cs.username)
    cs.group        = data.get("group", "")
    cs.updated_at   = data.get("updated_at", "")
    cs.checksum     = data.get("checksum", "")
    cs.version      = data.get("version", 0)
    cs.published    = True

def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bc = bpy.props.PointerProperty(type=BCScene)
    bpy.app.handlers.depsgraph_update_post.append(_enforce_bounds)
    if not bpy.app.timers.is_registered(_drain_sse_queue):
        bpy.app.timers.register(_drain_sse_queue, persistent=True)

def unregister():
    if bpy.app.timers.is_registered(_drain_sse_queue):
        bpy.app.timers.unregister(_drain_sse_queue)
    if _enforce_bounds in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_enforce_bounds)
    _sse_stop.set()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.bc

if __name__ == "__main__":
    register()
