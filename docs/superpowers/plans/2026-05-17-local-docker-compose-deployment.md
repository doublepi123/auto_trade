# Local Docker Compose Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start the existing Auto Trade application locally with Docker Compose and verify the backend health endpoint and frontend UI are reachable.

**Architecture:** Use the existing `docker-compose.yaml` without code changes. The backend runs as a FastAPI container on `127.0.0.1:8000`, the frontend runs as an Nginx container on `127.0.0.1:${AUTO_TRADE_FRONTEND_PORT:-8080}`, and SQLite data persists in `./data`.

**Tech Stack:** Docker Compose, FastAPI, Vue/Vite build served by Nginx, SQLite.

---

## File Structure

- Read: `.env` to confirm required runtime variables are present without printing secret values.
- Read: `docker-compose.yaml` to confirm service names, ports, health check, and required variables.
- No code files are modified for this deployment.
- No git commit is created unless the user explicitly asks for one, because repository policy requires explicit commit approval.

### Task 1: Validate Local Deployment Inputs

**Files:**
- Read: `.env`
- Read: `docker-compose.yaml`

- [ ] **Step 1: Confirm Docker is available**

Run: `docker compose version`
Expected: command exits 0 and prints a Docker Compose version.

- [ ] **Step 2: Confirm required `.env` variables exist without exposing values**

Run: `python3 - <<'PY'\nfrom pathlib import Path\nrequired = ['AUTO_TRADE_API_KEY', 'CREDENTIAL_MASTER_KEY']\noptional_credential_sets = [\n    ('LONGPORT_APP_KEY', 'LONGPORT_APP_SECRET', 'LONGPORT_ACCESS_TOKEN'),\n    ('LONGBRIDGE_APP_KEY', 'LONGBRIDGE_APP_SECRET', 'LONGBRIDGE_ACCESS_TOKEN'),\n]\nenv_path = Path('.env')\nif not env_path.exists():\n    raise SystemExit('.env is missing; create it from .env.example and fill required values')\nvalues = {}\nfor line in env_path.read_text().splitlines():\n    line = line.strip()\n    if not line or line.startswith('#') or '=' not in line:\n        continue\n    key, value = line.split('=', 1)\n    values[key.strip()] = value.strip().strip('"').strip("'")\nmissing = [key for key in required if not values.get(key)]\nif missing:\n    raise SystemExit('Missing required values: ' + ', '.join(missing))\nif not any(all(values.get(key) for key in keys) for keys in optional_credential_sets):\n    raise SystemExit('Missing complete Longbridge credentials: set LONGPORT_* or LONGBRIDGE_*')\nprint('Required deployment variables are present')\nPY`
Expected: prints `Required deployment variables are present`.

- [ ] **Step 3: Confirm Compose configuration resolves**

Run: `docker compose config --quiet`
Expected: command exits 0 with no output.

### Task 2: Start Services

**Files:**
- Read: `docker-compose.yaml`

- [ ] **Step 1: Build and start containers**

Run: `docker compose up --build -d`
Expected: Docker builds or reuses the backend and frontend images, then starts both services in detached mode.

- [ ] **Step 2: Inspect service state**

Run: `docker compose ps`
Expected: backend and frontend services are listed. Backend should be healthy or still starting briefly; frontend should be running after backend health passes.

### Task 3: Verify Deployment

**Files:**
- No file changes.

- [ ] **Step 1: Verify backend health endpoint**

Run: `curl -fsS http://localhost:8000/api/health`
Expected: JSON response containing `"ok":true` or `"ok": true`.

- [ ] **Step 2: Verify frontend serves HTML**

Run: `FRONTEND_URL="http://$(docker compose port frontend 80)/" && curl -fsS "$FRONTEND_URL"`
Expected: HTML response for the Auto Trade frontend.

- [ ] **Step 3: Inspect logs if verification fails**

Run: `docker compose logs --tail=100 backend frontend`
Expected: logs show the cause of any failed health check or startup error.

### Task 4: Report Access Details

**Files:**
- No file changes.

- [ ] **Step 1: Report service URLs**

Report:
- Frontend UI: output of `docker compose port frontend 80`
- Backend API: `http://localhost:8000`
- Health check: `http://localhost:8000/api/health`

- [ ] **Step 2: Report operational commands**

Report:
- Stop services: `docker compose down`
- View services: `docker compose ps`
- View logs: `docker compose logs -f backend frontend`

## Self-Review

- Spec coverage: the plan covers `.env` configuration, Docker Compose startup, backend health verification, frontend verification, local-only access, and rollback/stop commands.
- Completeness scan: all operational steps include exact commands and expected results.
- Type consistency: commands use the existing Compose service names `backend` and `frontend`, and the documented default frontend port `8080`.
