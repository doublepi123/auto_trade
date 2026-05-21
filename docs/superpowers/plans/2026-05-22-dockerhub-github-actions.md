# Docker Hub GitHub Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GitHub Actions workflow that builds and pushes the backend and frontend Docker images to Docker Hub.

**Architecture:** The repository already has independent backend and frontend Dockerfiles, so the workflow should build two separate images. Docker metadata generation should be centralized per image so default branch, SHA, and version tag pushes stay consistent.

**Tech Stack:** GitHub Actions YAML, Docker Buildx, Docker Hub, `docker/login-action`, `docker/metadata-action`, `docker/build-push-action`.

---

## File Structure

- Create `.github/workflows/dockerhub.yml`: GitHub Actions workflow that logs in to Docker Hub, builds `./backend` and `./frontend`, and pushes both images.
- Keep `docs/superpowers/specs/2026-05-22-dockerhub-github-actions-design.md`: design record for the workflow.

### Task 1: Create Docker Hub Workflow

**Files:**
- Create: `.github/workflows/dockerhub.yml`

- [ ] **Step 1: Create the workflow directory and file**

Create `.github/workflows/dockerhub.yml` with this content:

```yaml
name: Build and Push Docker Images

on:
  push:
    branches:
      - main
      - master
    tags:
      - 'v*'
  workflow_dispatch:

permissions:
  contents: read

env:
  BACKEND_IMAGE: ${{ secrets.DOCKERHUB_USERNAME }}/auto-trade-backend
  FRONTEND_IMAGE: ${{ secrets.DOCKERHUB_USERNAME }}/auto-trade-frontend

jobs:
  dockerhub:
    name: Build and push Docker images
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Extract backend metadata
        id: backend_meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.BACKEND_IMAGE }}
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=sha,prefix=sha-,format=short
            type=ref,event=tag

      - name: Build and push backend image
        uses: docker/build-push-action@v6
        with:
          context: ./backend
          file: ./backend/Dockerfile
          push: true
          tags: ${{ steps.backend_meta.outputs.tags }}
          labels: ${{ steps.backend_meta.outputs.labels }}

      - name: Extract frontend metadata
        id: frontend_meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.FRONTEND_IMAGE }}
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=sha,prefix=sha-,format=short
            type=ref,event=tag

      - name: Build and push frontend image
        uses: docker/build-push-action@v6
        with:
          context: ./frontend
          file: ./frontend/Dockerfile
          push: true
          tags: ${{ steps.frontend_meta.outputs.tags }}
          labels: ${{ steps.frontend_meta.outputs.labels }}
```

- [ ] **Step 2: Verify YAML parses**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml

path = Path('.github/workflows/dockerhub.yml')
with path.open() as handle:
    data = yaml.safe_load(handle)

assert data['name'] == 'Build and Push Docker Images'
assert 'dockerhub' in data['jobs']
print('workflow yaml ok')
PY
```

Expected output:

```text
workflow yaml ok
```

### Task 2: Final Verification

**Files:**
- Verify: `.github/workflows/dockerhub.yml`

- [ ] **Step 1: Confirm required workflow details**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

content = Path('.github/workflows/dockerhub.yml').read_text()
required = [
    'DOCKERHUB_USERNAME',
    'DOCKERHUB_TOKEN',
    './backend/Dockerfile',
    './frontend/Dockerfile',
    'auto-trade-backend',
    'auto-trade-frontend',
    'docker/build-push-action@v6',
]

missing = [item for item in required if item not in content]
assert not missing, missing
print('workflow content ok')
PY
```

Expected output:

```text
workflow content ok
```

## Self-Review

- Spec coverage: The plan creates the workflow, uses Docker Hub secrets, builds both existing Dockerfiles, pushes default-branch `latest`, SHA, and version tag refs.
- Placeholder scan: No placeholders remain.
- Type consistency: YAML step IDs and metadata output references match.
