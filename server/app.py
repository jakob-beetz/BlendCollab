"""
BlenderCollab Server v3 - FastAPI
===================================
Extends v2 with a server-side asset pipeline triggered on every publish.

Pipeline (runs in background after each upload):
  blender --background --python process_cell.py -- \
      --blend <raw.blend> --cell_id A1.3 \
      --origin_x 3 --origin_y 9 --parcel_size 3 \
      --out_assets <assets_dir>/cell_A1_3_assets.blend \
      --out_glb    <assets_dir>/cell_A1_3.glb

New endpoints:
  GET /api/cells/<id>/assets    download _assets.blend  (auth required)
  GET /api/cells/<id>/glb       download .glb           (public)
  GET /api/scene.json           all cells with GLB URLs + world origins (public)

Configure Blender binary:
  export BLENDER_BIN=/path/to/blender   (default: "blender")

Run:
  uv run uvicorn app:app --host 0.0.0.0 --port 5000 --reload
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, AsyncGenerator

import aiofiles
from fastapi import (
    Depends, FastAPI, File, Header, HTTPException,
    Query, UploadFile, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).parent
MODELS_DIR  = BASE_DIR / "models"
ASSETS_DIR  = BASE_DIR / "assets"   # _assets.blend + .glb files live here
DATA_FILE   = BASE_DIR / "state.json"
LAYOUT_FILE = BASE_DIR / "layout.json"
ROSTER_FILE = BASE_DIR / "roster.json"
STATIC_DIR  = BASE_DIR / "static"
PROCESS_SCRIPT = BASE_DIR / "process_cell.py"

for d in (MODELS_DIR, ASSETS_DIR, STATIC_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Blender binary - override with BLENDER_BIN env var
BLENDER_BIN: str = os.environ.get("BLENDER_BIN", "blender")

# ---------------------------------------------------------------------------
# Static config (loaded once at startup)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)

LAYOUT: dict       = _load_json(LAYOUT_FILE)
ROSTER: dict       = _load_json(ROSTER_FILE)
CELLS_DEF: dict    = LAYOUT["cells"]
ADJACENCY: dict    = LAYOUT["adjacency"]
GROUPS: dict       = LAYOUT["groups"]
PARCEL_SIZE: float = LAYOUT["parcel_size"]

ROSTER_BY_USERNAME: dict[str, dict] = {
    s["username"]: s
    for s in ROSTER["students"]
    if s.get("active", True)
}

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"users": {}, "cells": {}}

STATE: dict = _load_state()
_state_lock = asyncio.Lock()

async def _save_state() -> None:
    async with _state_lock:
        async with aiofiles.open(DATA_FILE, "w") as fh:
            await fh.write(json.dumps(STATE, indent=2))

# ---------------------------------------------------------------------------
# SSE pub/sub
# ---------------------------------------------------------------------------

_sse_queues: list[asyncio.Queue] = []
_sse_lock   = asyncio.Lock()

async def _broadcast(event: str, data: dict) -> None:
    payload = {"event": event, "data": data}
    async with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

# ---------------------------------------------------------------------------
# Asset pipeline
# ---------------------------------------------------------------------------

def _asset_stem(cell_id: str) -> str:
    """'A1.3' -> 'cell_A1_3'"""
    return "cell_" + cell_id.replace(".", "_")

def _assets_blend_path(cell_id: str) -> Path:
    return ASSETS_DIR / f"{_asset_stem(cell_id)}_assets.blend"

def _glb_path(cell_id: str) -> Path:
    return ASSETS_DIR / f"{_asset_stem(cell_id)}.glb"

async def _run_pipeline(cell_id: str, blend_path: Path) -> dict:
    """
    Spawns Blender headless to produce _assets.blend and .glb.
    Returns a dict with 'has_assets' and 'has_glb' booleans.
    Runs in a thread pool so it doesn't block the event loop.
    """
    cell_def   = CELLS_DEF[cell_id]
    origin     = cell_def["origin"]
    out_assets = _assets_blend_path(cell_id)
    out_glb    = _glb_path(cell_id)

    cmd = [
        BLENDER_BIN,
        "--background",
        "--python", str(PROCESS_SCRIPT),
        "--",
        "--blend",       str(blend_path),
        "--cell_id",     cell_id,
        "--origin_x",    str(origin[0]),
        "--origin_y",    str(origin[1]),
        "--parcel_size", str(PARCEL_SIZE),
        "--out_assets",  str(out_assets),
        "--out_glb",     str(out_glb),
    ]

    print(f"[pipeline] Starting for cell {cell_id}")

    def _run_sync():
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,   # 2 min max per cell
        )
        return result

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_sync)

    # Print Blender stdout/stderr so it shows in uvicorn logs
    if result.stdout:
        for line in result.stdout.splitlines():
            print(f"[blender/{cell_id}] {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            print(f"[blender/{cell_id}:err] {line}")

    has_assets = out_assets.exists()
    has_glb    = out_glb.exists()

    if result.returncode != 0:
        print(f"[pipeline] WARNING: Blender exited {result.returncode} for cell {cell_id}")
    else:
        print(f"[pipeline] Done: has_assets={has_assets} has_glb={has_glb}")

    return {"has_assets": has_assets, "has_glb": has_glb}

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    blender_found = shutil.which(BLENDER_BIN)
    print(f"BlenderCollab v3 - {len(CELLS_DEF)} cells, {len(ROSTER_BY_USERNAME)} students")
    print(f"Blender binary : {BLENDER_BIN} ({'found' if blender_found else 'NOT FOUND - pipeline disabled'})")
    print(f"Assets dir     : {ASSETS_DIR}")
    print(f"Docs           : http://localhost:5000/docs")
    yield

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BlenderCollab",
    description="Collaborative Blender parcel server - B-Atelier site plan",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def normalise(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("username must not be empty")
        return v


class RegisterResponse(BaseModel):
    token:        str
    cell_id:      str
    display_name: str
    origin:       list[float]
    group:        str
    group_color:  str
    neighbours:   list[str]
    message:      str


class PublishResponse(BaseModel):
    status:     str
    cell_id:    str
    version:    int
    checksum:   str
    has_assets: bool
    has_glb:    bool


class CellInfo(BaseModel):
    username:     str
    display_name: str
    cell_id:      str
    group:        str
    updated_at:   str
    checksum:     str
    version:      int
    filename:     str
    size_bytes:   int
    has_assets:   bool = False
    has_glb:      bool = False
    polygon:      list[list[float]] | None = None


class NeighbourEntry(BaseModel):
    cell_id:   str
    group:     str
    origin:    list[float]
    published: bool


class NeighboursResponse(BaseModel):
    cell_id:    str
    neighbours: list[NeighbourEntry]


class WhoAmIResponse(BaseModel):
    username:     str
    display_name: str
    cell_id:      str
    origin:       list[float]
    group:        str
    neighbours:   list[str]


class RosterStudent(BaseModel):
    username:     str
    display_name: str
    cell_id:      str
    active:       bool


class RosterResponse(BaseModel):
    students: list[RosterStudent]


class UserEntry(BaseModel):
    username:      str
    display_name:  str
    cell_id:       str
    group:         str
    registered_at: str


class UsersResponse(BaseModel):
    users: list[UserEntry]
    count: int

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def resolve_token(
    x_auth_token: Annotated[str | None, Header(alias="X-Auth-Token")] = None,
    token:        Annotated[str | None, Query()]                       = None,
) -> tuple[str, str]:
    t = x_auth_token or token
    if not t:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing auth token")
    entry = STATE["users"].get(t)
    if not entry:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid auth token")
    return t, entry["cell_id"]

AuthDep = Annotated[tuple[str, str], Depends(resolve_token)]

async def require_admin(
    x_admin_secret: Annotated[str | None, Header(alias="X-Admin-Secret")] = None,
) -> None:
    if x_admin_secret != os.environ.get("ADMIN_SECRET", "changeme"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

AdminDep = Annotated[None, Depends(require_admin)]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]

def _cell_polygon(cell_id: str) -> list[list[float]]:
    cell   = CELLS_DEF[cell_id]
    ox, oy = cell["origin"]
    s      = PARCEL_SIZE
    return [[ox, oy], [ox+s, oy], [ox+s, oy+s], [ox, oy+s]]

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Routes - Auth
# ---------------------------------------------------------------------------

@app.post("/api/register", response_model=RegisterResponse, tags=["Auth"])
async def register(body: RegisterRequest) -> RegisterResponse:
    """Register by username. Cell resolved from instructor roster."""
    username     = body.username
    roster_entry = ROSTER_BY_USERNAME.get(username)

    if not roster_entry:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Username '{username}' is not in the instructor roster. "
                "Check spelling or ask your instructor."
            ),
        )

    cell_id  = roster_entry["cell_id"]
    cell_def = CELLS_DEF[cell_id]

    for tok, info in STATE["users"].items():
        if info["username"] == username:
            return RegisterResponse(
                token=tok,
                cell_id=cell_id,
                display_name=roster_entry["display_name"],
                origin=cell_def["origin"],
                group=cell_def["group"],
                group_color=GROUPS[cell_def["group"]]["color"],
                neighbours=ADJACENCY.get(cell_id, []),
                message="Welcome back",
            )

    tok = secrets.token_hex(24)
    STATE["users"][tok] = {
        "username":      username,
        "display_name":  roster_entry["display_name"],
        "cell_id":       cell_id,
        "registered_at": _utcnow(),
    }
    await _save_state()

    await _broadcast("user_registered", {
        "username":     username,
        "display_name": roster_entry["display_name"],
        "cell_id":      cell_id,
        "group":        cell_def["group"],
    })

    return RegisterResponse(
        token=tok,
        cell_id=cell_id,
        display_name=roster_entry["display_name"],
        origin=cell_def["origin"],
        group=cell_def["group"],
        group_color=GROUPS[cell_def["group"]]["color"],
        neighbours=ADJACENCY.get(cell_id, []),
        message="Registered successfully",
    )

# ---------------------------------------------------------------------------
# Routes - Layout / Config
# ---------------------------------------------------------------------------

@app.get("/api/layout", tags=["Config"])
async def get_layout() -> dict:
    """Full layout.json - cell coordinates, adjacency, group colours."""
    return LAYOUT

@app.get("/api/scene.json", tags=["Config"])
async def scene_json() -> dict:
    """
    All published cells with their GLB URLs and world-space origins.
    Consumed by the Three.js dashboard viewer.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")
    cells_out = []
    for cell_id, cell_def in CELLS_DEF.items():
        info    = STATE["cells"].get(cell_id, {})
        has_glb = _glb_path(cell_id).exists()
        cells_out.append({
            "cell_id":      cell_id,
            "group":        cell_def["group"],
            "group_color":  GROUPS[cell_def["group"]]["color"],
            "origin":       cell_def["origin"],
            "parcel_size":  PARCEL_SIZE,
            "published":    bool(info),
            "has_glb":      has_glb,
            "glb_url":      f"{base}/api/cells/{cell_id}/glb" if has_glb else None,
            "display_name": info.get("display_name", ""),
            "updated_at":   info.get("updated_at", ""),
            "version":      info.get("version", 0),
        })
    return {
        "cells":        cells_out,
        "parcel_size":  PARCEL_SIZE,
        "site_bounds":  LAYOUT.get("site_bounds", {}),
        "groups":       GROUPS,
    }

# ---------------------------------------------------------------------------
# Routes - Publish (triggers pipeline)
# ---------------------------------------------------------------------------

@app.post("/api/publish", response_model=PublishResponse, tags=["Cells"])
async def publish(
    auth: AuthDep,
    file: UploadFile = File(...),
) -> PublishResponse:
    """
    Upload student .blend. Triggers headless Blender pipeline to produce
    _assets.blend and .glb. Pipeline runs async - does not block the response.
    """
    tok, cell_id = auth
    user_info    = STATE["users"][tok]

    raw      = await file.read()
    cs       = _checksum(raw)
    version  = int(time.time())
    cell_key = cell_id

    old_info = STATE["cells"].get(cell_key, {})
    filename = f"cell_{cell_id.replace('.', '_')}_v{version}.blend"
    dest     = MODELS_DIR / filename

    async with aiofiles.open(dest, "wb") as fh:
        await fh.write(raw)

    old_file = old_info.get("filename")
    if old_file and old_file != filename:
        (MODELS_DIR / old_file).unlink(missing_ok=True)

    STATE["cells"][cell_key] = {
        "username":     user_info["username"],
        "display_name": user_info["display_name"],
        "cell_id":      cell_id,
        "group":        CELLS_DEF[cell_id]["group"],
        "updated_at":   _utcnow(),
        "checksum":     cs,
        "version":      version,
        "filename":     filename,
        "size_bytes":   len(raw),
        "has_assets":   False,
        "has_glb":      False,
    }
    await _save_state()

    # Broadcast immediately so dashboard shows the publish
    await _broadcast("cell_updated", {
        **STATE["cells"][cell_key],
        "neighbours_of": ADJACENCY.get(cell_id, []),
        "pipeline":      "pending",
    })

    # Run pipeline in background - broadcast again when done
    asyncio.create_task(_pipeline_task(cell_id, dest, version))

    return PublishResponse(
        status="ok",
        cell_id=cell_id,
        version=version,
        checksum=cs,
        has_assets=False,
        has_glb=False,
    )


async def _pipeline_task(cell_id: str, blend_path: Path, version: int) -> None:
    """Background task: run Blender pipeline then broadcast updated state."""
    result = await _run_pipeline(cell_id, blend_path)

    cell_key = cell_id
    if cell_key in STATE["cells"] and STATE["cells"][cell_key]["version"] == version:
        STATE["cells"][cell_key]["has_assets"] = result["has_assets"]
        STATE["cells"][cell_key]["has_glb"]    = result["has_glb"]
        await _save_state()

        await _broadcast("cell_pipeline_done", {
            **STATE["cells"][cell_key],
            "neighbours_of": ADJACENCY.get(cell_id, []),
            "pipeline":      "done",
        })

# ---------------------------------------------------------------------------
# Routes - Cells
# ---------------------------------------------------------------------------

@app.get("/api/cells", tags=["Cells"])
async def list_cells() -> dict:
    return {
        "cells":       STATE["cells"],
        "parcel_size": PARCEL_SIZE,
        "total_cells": len(CELLS_DEF),
    }


@app.get("/api/cells/{cell_id:path}/info", response_model=CellInfo, tags=["Cells"])
async def cell_info(cell_id: str) -> CellInfo:
    info = STATE["cells"].get(cell_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"No model published for '{cell_id}'")
    return CellInfo(**info, polygon=_cell_polygon(cell_id))


@app.get("/api/cells/{cell_id:path}/neighbours",
         response_model=NeighboursResponse, tags=["Cells"])
async def cell_neighbours(cell_id: str) -> NeighboursResponse:
    if cell_id not in CELLS_DEF:
        raise HTTPException(status_code=404, detail=f"Unknown cell_id '{cell_id}'")
    return NeighboursResponse(
        cell_id=cell_id,
        neighbours=[
            NeighbourEntry(
                cell_id=nid,
                group=CELLS_DEF[nid]["group"],
                origin=CELLS_DEF[nid]["origin"],
                published=nid in STATE["cells"],
            )
            for nid in ADJACENCY.get(cell_id, [])
        ],
    )


@app.get("/api/cells/{cell_id:path}/download", tags=["Cells"])
async def download_cell(cell_id: str, auth: AuthDep) -> FileResponse:
    """Download raw .blend (auth required)."""
    info = STATE["cells"].get(cell_id)
    if not info:
        raise HTTPException(status_code=404, detail="No model published for this cell")
    filepath = MODELS_DIR / info["filename"]
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")
    safe_id = cell_id.replace(".", "_")
    return FileResponse(
        path=str(filepath),
        filename=f"cell_{safe_id}_{info['username']}.blend",
        media_type="application/octet-stream",
    )


@app.get("/api/cells/{cell_id:path}/assets", tags=["Cells"])
async def download_assets(cell_id: str, auth: AuthDep) -> FileResponse:
    """
    Download the _assets.blend for a cell (auth required).
    Used by the addon to auto-link the cell's collection asset.
    """
    p = _assets_blend_path(cell_id)
    if not p.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Assets not yet generated for '{cell_id}' - pipeline may still be running"
        )
    safe_id = cell_id.replace(".", "_")
    return FileResponse(
        path=str(p),
        filename=f"cell_{safe_id}_assets.blend",
        media_type="application/octet-stream",
    )


@app.get("/api/cells/{cell_id:path}/glb", tags=["Cells"])
async def download_glb(cell_id: str) -> FileResponse:
    """
    Download the .glb for a cell (no auth - used by Three.js viewer).
    """
    p = _glb_path(cell_id)
    if not p.exists():
        raise HTTPException(
            status_code=404,
            detail=f"GLB not yet generated for '{cell_id}'"
        )
    safe_id = cell_id.replace(".", "_")
    return FileResponse(
        path=str(p),
        filename=f"cell_{safe_id}.glb",
        media_type="model/gltf-binary",
    )

# ---------------------------------------------------------------------------
# Routes - SSE
# ---------------------------------------------------------------------------

@app.get("/api/events", tags=["SSE"])
async def sse_stream(
    filter:  Annotated[str, Query()] = "all",
    cell_id: Annotated[str, Query()] = "",
) -> StreamingResponse:
    """
    SSE stream. Events: connected, cell_updated, cell_pipeline_done,
    user_registered, cell_reset. Heartbeat every 25s.
    filter=neighbours&cell_id=A1.3 for neighbour-only stream.
    """
    my_neighbours: set[str] = (
        set(ADJACENCY.get(cell_id, [])) | {cell_id} if cell_id else set()
    )

    q: asyncio.Queue = asyncio.Queue(maxsize=120)
    async with _sse_lock:
        _sse_queues.append(q)

    async def generate() -> AsyncGenerator[str, None]:
        yield f"event: connected\ndata: {json.dumps({'total_cells': len(CELLS_DEF)})}\n\n"
        try:
            while True:
                try:
                    msg        = await asyncio.wait_for(q.get(), timeout=25.0)
                    event      = msg["event"]
                    data       = msg["data"]
                    event_cell = data.get("cell_id", "")
                    if filter == "neighbours" and my_neighbours:
                        if event_cell not in my_neighbours:
                            continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            async with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )

# ---------------------------------------------------------------------------
# Routes - Users / Roster
# ---------------------------------------------------------------------------

@app.get("/api/users", response_model=UsersResponse, tags=["Users"])
async def list_users() -> UsersResponse:
    users = sorted(
        [
            UserEntry(
                username=info["username"],
                display_name=info["display_name"],
                cell_id=info["cell_id"],
                group=CELLS_DEF.get(info["cell_id"], {}).get("group", ""),
                registered_at=info["registered_at"],
            )
            for info in STATE["users"].values()
        ],
        key=lambda u: u.cell_id,
    )
    return UsersResponse(users=users, count=len(users))


@app.get("/api/whoami", response_model=WhoAmIResponse, tags=["Users"])
async def whoami(auth: AuthDep) -> WhoAmIResponse:
    tok, cell_id = auth
    info         = STATE["users"][tok]
    cell_def     = CELLS_DEF[cell_id]
    return WhoAmIResponse(
        username=info["username"],
        display_name=info["display_name"],
        cell_id=cell_id,
        origin=cell_def["origin"],
        group=cell_def["group"],
        neighbours=ADJACENCY.get(cell_id, []),
    )


@app.get("/api/roster", response_model=RosterResponse, tags=["Users"])
async def get_roster() -> RosterResponse:
    return RosterResponse(
        students=[
            RosterStudent(
                username=s["username"],
                display_name=s["display_name"],
                cell_id=s["cell_id"],
                active=s.get("active", True),
            )
            for s in ROSTER["students"]
        ]
    )


@app.get("/api/roster/check", tags=["Users"])
async def roster_check(username: Annotated[str, Query()]) -> dict:
    """Check a username against the roster. Returns suggestions on mismatch."""
    normalised = username.strip().lower()
    entry      = ROSTER_BY_USERNAME.get(normalised)
    if entry:
        return {"found": True, "username": normalised,
                "display_name": entry["display_name"], "cell_id": entry["cell_id"]}
    import difflib
    close = difflib.get_close_matches(normalised, ROSTER_BY_USERNAME.keys(), n=3, cutoff=0.6)
    return {"found": False, "username": normalised, "suggestions": close,
            "hint": f"Did you mean one of {close}?" if close else "No similar usernames found."}

# ---------------------------------------------------------------------------
# Routes - Admin
# ---------------------------------------------------------------------------

@app.delete("/api/reset_cell/{cell_id:path}", tags=["Admin"])
async def reset_cell(cell_id: str, _: AdminDep) -> dict:
    """Clear a cell: removes .blend, _assets.blend, .glb and state entry."""
    info = STATE["cells"].pop(cell_id, None)
    if info and info.get("filename"):
        (MODELS_DIR / info["filename"]).unlink(missing_ok=True)
    _assets_blend_path(cell_id).unlink(missing_ok=True)
    _glb_path(cell_id).unlink(missing_ok=True)
    await _save_state()
    await _broadcast("cell_reset", {"cell_id": cell_id})
    return {"status": "ok", "cell_id": cell_id}

# ---------------------------------------------------------------------------
# Static / Dashboard
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve assets dir so Three.js can fetch GLBs directly if needed
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets_files")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"detail": "Place index.html in server/static/"}, status_code=404)

# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
