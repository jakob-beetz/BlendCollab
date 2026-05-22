# BlenderCollab v3 — B-Atelier Site Plan System

Collaborative Blender environment for **34 students** working on 1:1 scale art
installations on the real B-Atelier plot. Cells are named by group + position
(`A1.3`, `B2.7`, etc.), assigned by the instructor via `roster.json`.

The server is built on **FastAPI** and runs in a **Docker** container with
**Blender 5.x** headless for server-side GLB export.

---

## Directory Structure

```
BlendColab/
├── Dockerfile               ← Blender 5.x + Python 3.11 image
├── server/
│   ├── app.py               ← FastAPI server (v3)
│   ├── process_cell.py      ← Headless Blender pipeline script
│   ├── requirements.txt
│   ├── layout.json          ← Cell coordinates + adjacency graph
│   ├── roster.json          ← Instructor-controlled: username → cell assignment
│   ├── state.json           ← Auto-created: tokens + published cell metadata
│   ├── models/              ← Auto-created: raw .blend uploads per cell
│   ├── assets/              ← Auto-created: _assets.blend + .glb per cell
│   ├── snapshots/           ← Auto-created: daily .tar.gz environment snapshots
│   └── static/
│       └── index.html       ← Three.js site plan dashboard
└── addon/
    └── blender_collab_v3.py ← Blender 5.x addon
```

---

## Quick Start

### 1. Edit the roster

Open `server/roster.json` and replace `student01` … `student34` with your
actual student usernames and their assigned cells.

```json
{ "username": "alice", "display_name": "Alice M.", "cell_id": "A1.3", "active": true }
```

### 2. Start the server

```bash
# Development (local)
cd server
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 5000 --reload

# Production (Docker)
docker build -t blendcolab-prod .
docker run -d --name blendcolab-prod --restart unless-stopped \
  -p 5000:5000 \
  -v $(pwd)/server/models:/app/server/models \
  -v $(pwd)/server/assets:/app/server/assets \
  -v $(pwd)/server/snapshots:/app/server/snapshots \
  -v $(pwd)/server/state.json:/app/server/state.json \
  blendcolab-prod
# Dashboard → http://localhost:5000/
# API docs  → http://localhost:5000/docs
```

### 3. Install the addon in Blender

**Edit → Preferences → Add-ons → Install** → select `addon/blender_collab_v3.py`

Set the **Server URL** in the addon preferences (Edit → Preferences → Add-ons → BlenderCollab v3).

### 4. Student workflow

1. Open your shared environment `.blend` file (provided by instructor)
2. N-panel → **BlenderCollab** → enter your username → **Register / Connect**
3. Your cell boundary appears in **green**; direct neighbours in **amber**; others in dim red
4. Work within your green boundary (objects snapped back automatically if moved outside)
5. Click **Save & Publish** to upload to the server
6. Watch the **Cell Updates** panel — cells marked `!` have new content
7. Toggle **All cells / Neighbours** filter to reduce noise
8. Click `↓` next to any cell to download and import it (objects are locked after import)

---

## Cell Layout (from site plan)

```
Group A1 (pink):
  Row +4:  A1.1  A1.2
  Row +3:  A1.3  A1.4  A1.5  A1.6
  Row +2:              A1.7  A1.8  A1.9

Group A2 (blue), adjacent right of A1:
  Row +3:  A2.1  A2.2  A2.3  A2.4
  Row +2:  A2.5  A2.6  A2.7  A2.8

Group B1 (green), below A1:
  Row +1:  B1.1        B1.2  B1.3
  Row  0:  B1.4  B1.5  B1.6  B1.7
  Row -1:        B1.8  B1.9

Group B2 (yellow), right of B1 / below A2:
  Row +2:                          B2.1
  Row +1:                          B2.2
  Row  0:              B2.3  B2.4  B2.5
  Row -1:              B2.6  B2.7  B2.8
```

Total: **34 cells** · Each 3 × 3 m · Overall site approx 27 × 18 m

---

## Adjusting Cell Coordinates

`layout.json` contains the cell origins in world-space metres.  
When you have the actual surveyed coordinates from the site plan:

1. Open `layout.json`
2. For each cell, update `"origin": [x, y]` (bottom-left corner in metres)
3. Update `"site_bounds"` to match the full extent
4. Update `"adjacency"` if any cell neighbours change
5. Restart the server — the addon and dashboard both fetch layout at startup

---

## Dashboard Features

- **SVG site map** — cells drawn at real proportions, colour-coded by group
- **Hover** any cell to see author, version, timestamp, file size
- **Hover** highlights all direct neighbours with a white outline
- **Yellow badge** appears on cells with unpublished updates
- **Flash animation** when a cell receives a new publish
- **Activity log** sidebar — live stream of all events
- **Roster panel** — all students with their cells, coloured badges for published/updated

---

## API Reference

### Student / Public

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/register` | — | Register by username (cell resolved from roster) |
| GET | `/api/layout` | — | Full layout.json with adjacency |
| GET | `/api/scene.json` | — | All cells with GLB URLs + world origins (Three.js) |
| POST | `/api/publish` | token | Upload .blend → queues Blender pipeline |
| GET | `/api/cells` | — | All published cell metadata |
| GET | `/api/cells/<id>/info` | — | Single cell metadata + polygon |
| GET | `/api/cells/<id>/neighbours` | — | Neighbour metadata |
| GET | `/api/cells/<id>/download` | token | Download raw .blend file |
| GET | `/api/cells/<id>/assets` | token | Download _assets.blend (for addon linking) |
| GET | `/api/cells/<id>/glb` | — | Download .glb (used by Three.js viewer) |
| GET | `/api/events` | — | SSE stream (supports `?filter=neighbours&cell_id=A1.3`) |
| GET | `/api/roster` | — | Public roster |
| GET | `/api/roster/check` | — | Username lookup with fuzzy suggestions |
| GET | `/api/users` | — | Registered users |
| GET | `/api/whoami` | token | Your identity + neighbours |

### Admin (require `X-Admin-Secret` header)

| Method | Path | Description |
|--------|------|-------------|
| DELETE | `/api/reset_cell/<id>` | Clear a single cell (blend + glb + state) |
| POST | `/api/reset_scene` | Clear **all** cells — full scene reset |
| POST | `/api/reprocess` | Re-run Blender pipeline for all cells |
| POST | `/api/reprocess/<id>` | Re-run Blender pipeline for one cell |
| POST | `/api/snapshot` | Manually take a snapshot of the full environment |
| GET | `/api/snapshots` | List available snapshots |
| GET | `/api/snapshots/<filename>` | Download a snapshot `.tar.gz` |

### SSE Filter

The addon connects to SSE with a filter parameter:

```
/api/events?filter=neighbours&cell_id=A1.3
```

Server-side filtering means only events for `A1.3`'s direct neighbours are pushed,
reducing load for students who only want to see their immediate context.

---

## Environment .blend Integration

The instructor provides a shared environment `.blend` file (buildings, terrain,
pathways). Students open this file before registering. The addon layers on top:

- Cell boundaries are drawn as wire objects (not exportable as geometry)
- Student objects are tagged `bc_cell = "A1.3"` etc.
- On download, old imported objects for that cell are removed before new ones added
- Boundary objects tagged `bc_boundary = True` are excluded from publish

To update the environment (e.g. fix the terrain), the instructor can publish it
through a special admin cell or simply redistribute the `.blend` file.

---

## Production Notes

- **FastAPI / Uvicorn** — SSE works natively; no gevent needed
- **ADMIN_SECRET** env var controls all admin endpoints (default: `changeme` — change in production)
- **BLENDER_BIN** env var overrides the Blender binary path (default: `blender`)
- **SNAPSHOT_INTERVAL_HOURS** env var sets the auto-snapshot frequency (default: `24`)
- `state.json`, `models/`, `assets/`, and `snapshots/` should be bind-mounted so data persists across container restarts
- `.blend` files can be large; set `client_max_body_size 500m` in Nginx if proxying
- Interactive API docs at `/docs` (Swagger UI) and `/redoc`
- The Blender pipeline is limited to 3 concurrent processes (`_pipeline_sem`); adjust in `app.py` for your RAM
