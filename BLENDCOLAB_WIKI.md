# BlendColab Local Setup & Deployment (Docker)

## Local Development
- Clone repo and `cd` into project root.
- Build Docker image (only needed after Dockerfile/requirements.txt/Blender changes):
  ```sh
  docker build -t blendcolab-dev .
  ```
- Run with live code mounting:
  ```sh
  docker run --rm -it -p 5000:5000 \
    -v $(pwd)/server/models:/app/server/models \
    -v $(pwd)/server/assets:/app/server/assets \
    -v $(pwd)/server/snapshots:/app/server/snapshots \
    -v $(pwd)/server/state.json:/app/server/state.json \
    blendcolab-dev
  ```
- App available at http://localhost:5000
- API docs at http://localhost:5000/docs

## Production Deployment
- Copy project files (Dockerfile, server/, static/, etc.) to server.
- Create persistent data directories on the host (first deploy only):
  ```sh
  mkdir -p ~/blendcollab/server/{models,assets,snapshots}
  touch ~/blendcollab/server/state.json
  ```
- Build image on server:
  ```sh
  cd ~/blendcollab && docker build -t blendcolab-prod .
  ```
- Run container on port 5000 (adjust if port is taken):
  ```sh
  docker run -d --name blendcolab-prod --restart unless-stopped \
    -p 5000:5000 \
    -v ~/blendcollab/server/models:/app/server/models \
    -v ~/blendcollab/server/assets:/app/server/assets \
    -v ~/blendcollab/server/snapshots:/app/server/snapshots \
    -v ~/blendcollab/server/state.json:/app/server/state.json \
    blendcolab-prod
  ```
- Set up Nginx/Apache reverse proxy to forward domain to port 5000.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_SECRET` | `changeme` | Secret for all admin API endpoints (`X-Admin-Secret` header) |
| `BLENDER_BIN` | `blender` | Path to Blender binary |
| `SNAPSHOT_INTERVAL_HOURS` | `24` | How often automatic snapshots are taken |
| `PUBLIC_BASE_URL` | `http://localhost:5000` | Base URL used in `scene.json` GLB links |

## Updating Code (No Rebuild Needed)
- For prod: sync updated Python files and restart the container:
  ```sh
  rsync -av -e "ssh -i ~/.ssh/your-key" Dockerfile server/app.py server/process_cell.py \
    user@server:~/blendcollab/
  ssh -i ~/.ssh/your-key user@server \
    "cd ~/blendcollab && docker build -t blendcolab-prod . && docker restart blendcolab-prod"
  ```
- Only rebuild image if Dockerfile or requirements.txt change.

## Admin Operations

All admin endpoints require the `X-Admin-Secret` header matching `ADMIN_SECRET`.

```sh
# Re-run Blender pipeline for all cells (e.g. after updating process_cell.py)
curl -X POST http://server:5000/api/reprocess -H "X-Admin-Secret: changeme"

# Re-run pipeline for a single cell
curl -X POST http://server:5000/api/reprocess/B1.5 -H "X-Admin-Secret: changeme"

# Take a manual environment snapshot (state + models + assets → .tar.gz)
curl -X POST http://server:5000/api/snapshot -H "X-Admin-Secret: changeme"

# List snapshots
curl http://server:5000/api/snapshots -H "X-Admin-Secret: changeme"

# Download a snapshot
curl -O http://server:5000/api/snapshots/snapshot_20260522T120000Z_daily.tar.gz \
  -H "X-Admin-Secret: changeme"

# Reset a single cell
curl -X DELETE http://server:5000/api/reset_cell/B1.5 -H "X-Admin-Secret: changeme"

# Reset the entire scene (destructive — snapshot first!)
curl -X POST http://server:5000/api/reset_scene -H "X-Admin-Secret: changeme"
```

## Snapshots

The server automatically creates a `.tar.gz` snapshot every 24 hours (configurable via
`SNAPSHOT_INTERVAL_HOURS`). Each snapshot contains:
- `state.json` — all tokens and cell metadata
- `models/*.blend` — raw student uploads
- `assets/` — generated `_assets.blend` and `.glb` files

Snapshots are stored in `server/snapshots/` (bind-mounted to the host).
Use `POST /api/snapshot` to trigger one manually before risky operations.
