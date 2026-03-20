# Setting up BlenderCollab with uv

[uv](https://docs.astral.sh/uv/) is a fast Python package and project manager
written in Rust. It replaces `pip`, `venv`, and `pip-tools` in a single tool and
is the recommended way to manage this project.

---

## 1 — Install uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your shell, then verify:

```bash
uv --version
# uv 0.x.x
```

---

## 2 — Create the project

```bash
cd blender_collab_v2/server

# Initialise a uv project in-place (adds pyproject.toml + uv.lock)
uv init --no-workspace --name blender-collab .
```

`--no-workspace` prevents uv from scanning parent directories for a workspace root.

---

## 3 — Add dependencies

```bash
uv add "fastapi>=0.111.0" "uvicorn[standard]>=0.29.0" "python-multipart>=0.0.9" "aiofiles>=23.2.1"
```

`uv add` does three things in one command:
1. Resolves the full dependency tree
2. Writes pinned versions to `uv.lock`
3. Installs everything into `.venv/`

Your `pyproject.toml` will now contain:

```toml
[project]
name = "blender-collab"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiofiles>=23.2.1",
    "fastapi>=0.111.0",
    "python-multipart>=0.0.9",
    "uvicorn[standard]>=0.29.0",
]
```

The old `requirements.txt` is no longer needed.  
**Commit both `pyproject.toml` and `uv.lock` to version control.**

---

## 4 — Run the server

```bash
# Development — auto-reloads on file changes
uv run uvicorn app:app --host 0.0.0.0 --port 5000 --reload

# Production — no reload, workers via --workers (requires uvicorn[standard])
uv run uvicorn app:app --host 0.0.0.0 --port 5000 --workers 1
```

`uv run` automatically activates the project's virtual environment for that
command — you never need to `source .venv/bin/activate` manually.

Open **http://localhost:5000/docs** for the interactive Swagger UI.

---

## 5 — Reproduce the environment on another machine

When a colleague (or a server) checks out the repo:

```bash
# Installs exactly the versions recorded in uv.lock — no resolution needed
uv sync
```

Then run as normal:

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 5000
```

---

## 6 — Useful day-to-day commands

| Task | Command |
|------|---------|
| Add a package | `uv add <package>` |
| Remove a package | `uv remove <package>` |
| Upgrade a package | `uv add "<package>>=<new_version>"` |
| Upgrade all packages | `uv lock --upgrade && uv sync` |
| Run any command in the venv | `uv run <command>` |
| Open a Python shell in the venv | `uv run python` |
| Show installed packages | `uv pip list` |
| Export a requirements.txt (e.g. for Docker) | `uv export --format requirements-txt > requirements.txt` |

---

## 7 — Pinning the Python version (optional but recommended)

```bash
# Tell uv which Python version this project requires
uv python pin 3.12
```

This creates a `.python-version` file. uv will download and use that exact
CPython release automatically if it isn't already installed.

---

## 8 — Docker (optional)

```dockerfile
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first (layer cache)
COPY pyproject.toml uv.lock ./

# Install dependencies only (no project itself)
RUN uv sync --frozen --no-install-project

# Copy the rest of the application
COPY . .

EXPOSE 5000
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
```

`--frozen` makes `uv sync` fail if `uv.lock` is out of date rather than
silently re-resolving — important for reproducible production builds.

---

## Project structure after setup

```
server/
├── .venv/                  ← created by uv, do not commit (add to .gitignore)
├── .python-version         ← optional Python pin, commit this
├── pyproject.toml          ← project metadata + dependencies, commit this
├── uv.lock                 ← pinned full dependency tree, commit this
├── app.py
├── layout.json
├── roster.json
├── state.json              ← runtime, do not commit
├── models/                 ← runtime, do not commit
└── static/
    └── index.html
```

Add to `.gitignore`:
```
.venv/
state.json
models/
__pycache__/
*.pyc
```
