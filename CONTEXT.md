# BlenderCollab — Project Context for AI Assistant

## What this project is

A real-time collaborative system for a university art studio course (B-Atelier).
34 students each work on a **1:1 scale 3D art installation** in Blender. Each student
owns a dedicated **3×3 m parcel** on a real plot of land. The system keeps everyone
in sync: students publish their work to a central server, see neighbours' updates
live, and the instructor monitors all cells from a web dashboard.

---

## Project structure

```
blender_collab_v3/
├── addon/
│   └── blender_collab_v3.py      # Blender 4.x/5.x Python addon (1116 lines)
└── server/
    ├── app.py                     # FastAPI server (807 lines)
    ├── process_cell.py            # Headless Blender pipeline script (187 lines)
    ├── layout.json                # Cell coordinates + adjacency graph (100 lines)
    ├── roster.json                # Instructor-controlled username→cell map (51 lines)
    ├── pyproject.toml             # uv project file
    ├── requirements.txt           # Kept for reference; uv/pyproject.toml is canonical
    ├── .gitignore
    └── static/
        └── index.html             # Dashboard: Three.js 3D view + SVG plan (657 lines)

# Runtime-generated (not committed):
    ├── state.json                 # User tokens + published cell metadata
    ├── models/                    # Raw .blend files from students
    └── assets/                    # Pipeline output: _assets.blend + .glb per cell
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Server | Python 3.12, FastAPI, uvicorn, aiofiles |
| Package management | uv (`uv sync`, `uv run uvicorn app:app ...`) |
| Real-time | Server-Sent Events (SSE) via FastAPI `StreamingResponse` |
| 3D pipeline | Headless Blender (`blender --background --python process_cell.py`) |
| Dashboard | Vanilla JS + Three.js r128 + SVG |
| Blender addon | Pure Python, stdlib only (no pip inside Blender) |

---

## Key design decisions

### Cell IDs
Alphanumeric strings: `"A1.1"` through `"A1.9"`, `"A2.1"` through `"A2.8"`,
`"B1.1"` through `"B1.9"`, `"B2.1"` through `"B2.8"`. 34 cells total across
4 groups (A1=pink, A2=blue, B1=green, B2=yellow). Defined in `layout.json`.

### Auth
Instructor pre-assigns students in `roster.json`. Students register by username
only — the server resolves their cell. Returns a 48-char hex token stored in
`state.json`. All subsequent requests use `X-Auth-Token` header.

### SSE pub/sub
Pure asyncio: `asyncio.Queue` per connected client, `_broadcast()` puts events
onto all queues. No Redis, no threads. Single uvicorn worker required.

SSE events:
- `cell_updated` — student published (pipeline=pending)
- `cell_pipeline_done` — Blender pipeline finished (has_glb, has_assets flags)
- `user_registered` — new student connected
- `cell_reset` — admin cleared a cell
- `: heartbeat` — every 25s keepalive

Clients can filter: `GET /api/events?filter=neighbours&cell_id=A1.3`

### Pipeline (publish flow)
1. Student clicks "Save & Publish" in Blender addon
2. POST `/api/publish` — server saves `.blend`, responds immediately
3. Server broadcasts `cell_updated` (pipeline=pending) via SSE
4. `asyncio.create_task(_pipeline_task(...))` runs in background
5. `_pipeline_task` calls `blender --background --python process_cell.py`
   via `loop.run_in_executor(None, subprocess.run)`
6. `process_cell.py` produces:
   - `assets/cell_A1_3_assets.blend` — Collection Asset for Blender linking
   - `assets/cell_A1_3.glb` — for Three.js dashboard viewer
7. Server broadcasts `cell_pipeline_done` with `has_glb=True`
8. Dashboard loads GLB into Three.js scene at correct world position
9. Neighbour students' addons auto-link the `_assets.blend`

### Blender addon asset linking
When `cell_pipeline_done` arrives via SSE for a neighbour cell:
- Downloads `_assets.blend` to local `bc_assets/` folder
- Uses `bpy.data.libraries.load(..., link=True)` to link the collection
- Creates library overrides to set world position (origin from `layout.json`)
- Locks all transforms on linked objects (`lock_location/rotation/scale = True`)
- Tags objects: `obj["bc_cell"] = "A1.3"`, `obj["bc_imported"] = True`

Student's own objects are tagged `bc_cell = own_id`, boundary helpers tagged
`bc_boundary = True`. These are excluded from publish uploads.

### Parcel enforcement
`depsgraph_update_post` handler checks every object after each transform.
Uses **point-in-polygon ray-casting** (not AABB) so it works for any shape.
Objects moved outside the 3×3 m boundary are snapped back in the same frame.

### Dashboard 3D view
Three.js r128 + GLTFLoader from jsdelivr CDN.
- World coordinates: Three.js X = Blender X, Three.js Z = −Blender Y (Y-up flip)
- GLBs are exported at origin 0,0; positioned in Three.js at `(ox, 0, -oy)`
- Floor plates always visible, coloured by group, opacity increases when GLB loads
- Orbit: mouse drag = rotate, scroll = zoom

---

## API endpoints

```
POST /api/register              body: {username}  → {token, cell_id, origin, neighbours, group...}
GET  /api/layout                → full layout.json
GET  /api/scene.json            → all cells with GLB URLs + origins (for Three.js)
POST /api/publish               multipart file + X-Auth-Token → {status, version, checksum}
GET  /api/cells                 → all published cell metadata
GET  /api/cells/{id}/info       → single cell + polygon vertices
GET  /api/cells/{id}/neighbours → direct neighbours with publish status
GET  /api/cells/{id}/download   → raw .blend (auth required)
GET  /api/cells/{id}/assets     → _assets.blend (auth required)
GET  /api/cells/{id}/glb        → .glb (public, used by Three.js)
GET  /api/events                → SSE stream
GET  /api/users                 → registered users
GET  /api/whoami                → current user info (auth required)
GET  /api/roster                → public roster
GET  /api/roster/check?username=x → check username, returns suggestions on mismatch
DELETE /api/reset_cell/{id}     → admin: clear cell (X-Admin-Secret header)
```

---

## Running the server

```bash
cd server

# First time setup
uv sync

# Development
uv run uvicorn app:app --host 0.0.0.0 --port 5000 --reload

# Set Blender binary (required for pipeline)
export BLENDER_BIN=/path/to/blender

# Admin secret for reset endpoint (default: "changeme")
export ADMIN_SECRET=mysecret

# Dashboard
http://localhost:5000/

# API docs (Swagger UI)
http://localhost:5000/docs
```

---

## Blender addon

File: `addon/blender_collab_v3.py`
Install via Edit → Preferences → Add-ons → Install.
Set server URL in addon preferences (not in the panel — that was a bug that was fixed).

**Operators registered:**
- `bc.register` — connects to server, fetches layout, starts SSE, draws boundaries
- `bc.publish` — saves scene (auto-saves if unsaved), uploads, triggers pipeline
- `bc.draw_bounds` — redraws wire boundaries (green=own, amber=neighbours, red=others)
- `bc.link_cell_assets` — downloads + links `_assets.blend` for one cell
- `bc.relink_all_neighbours` — re-links all neighbour cells in one click
- `bc.download_cell` — legacy raw .blend download + append
- `bc.set_filter` — reconnects SSE with new filter (all/neighbours)
- `bc.refresh_cells` — manual cell list refresh from server

**N-panel location:** View3D → Sidebar (N) → BlenderCollab tab

---

## layout.json structure

```json
{
  "parcel_size": 3.0,
  "groups": { "A1": {"color": "#e87ea1", "label": "Group A1"}, ... },
  "cells": {
    "A1.1": { "group": "A1", "origin": [3, 12], "col": 1, "row": 4 },
    ...
  },
  "adjacency": {
    "A1.1": ["A1.2", "A1.3", "A1.4"],
    ...
  },
  "site_bounds": { "min_x": 3, "min_y": -3, "max_x": 30, "max_y": 15 }
}
```

`origin` is the world-space bottom-left corner of each cell in metres.
`adjacency` is the hand-crafted neighbour graph including cross-group edges.

---

## roster.json structure

```json
{
  "students": [
    { "username": "anna", "display_name": "Anna K.", "cell_id": "A1.1", "active": true },
    ...
  ]
}
```

Usernames are lowercased on lookup. Edit this file and restart the server
before each session. Students type their username exactly as it appears here.

---

## Known issues / things to watch for

- **`BLENDER_BIN` must be set** or the pipeline silently skips. Check server
  startup logs: `"Blender binary: blender (found)"` vs `"NOT FOUND"`.
- **Single worker only** for SSE to work correctly. `uvicorn --workers 1` or
  `gunicorn -k uvicorn.workers.UvicornWorker -w 1`.
- **Library Override API** (`obj.override_create()`) requires Blender 3.0+.
  On Blender 5.x the API is stable but behaviour changed slightly in 4.2+.
- **Icon names changed** between Blender 4.x and 5.x. `CLOUD_DATA` no longer
  exists in 5.1 — replaced with `EXPORT`. Always check against
  `bpy.types.UILayout.bl_rna.properties["icon"].enum_items` if adding new icons.
- **state.json** is the only persistence. Back it up between sessions to preserve
  student tokens. If deleted, all students must re-register.
- **GLB coordinate flip**: Blender uses Z-up, Three.js Y-up. `process_cell.py`
  passes `export_yup=True` to the glTF exporter. In Three.js, cell position is
  `model.position.set(ox, 0, -oy)` — note the negated Y→Z.
- **process_cell.py arg parsing**: uses `sys.argv` split on `"--"`. Everything
  before `--` is consumed by Blender itself.
