# Docker Hub GitHub Actions Design

## Goal

Publish production Docker images to Docker Hub from GitHub Actions whenever repository changes are merged or a release tag is pushed.

## Current Context

The project already has separate Docker build contexts for the two runtime services:

- `backend/Dockerfile` builds the FastAPI backend image.
- `frontend/Dockerfile` builds the Vue/Nginx frontend image.
- `docker-compose.yaml` runs the services as separate `backend` and `frontend` containers.

There is currently no `.github/workflows` directory.

## Chosen Approach

Create one GitHub Actions workflow at `.github/workflows/dockerhub.yml` with one build-and-push job for both images.

The workflow will push these Docker Hub repositories by default:

- `${{ secrets.DOCKERHUB_USERNAME }}/auto-trade-backend`
- `${{ secrets.DOCKERHUB_USERNAME }}/auto-trade-frontend`

It expects the repository secrets already configured by the user:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

## Triggers

The workflow runs on:

- Pushes to `main` and `master`.
- Version tags matching `v*`.
- Manual `workflow_dispatch` runs.

## Tags

Each image gets:

- `latest` for default-branch builds.
- `sha-<short commit sha>` for traceable builds.
- The Git tag name for tag builds, such as `v1.0.0`.

## Implementation Notes

Use official Docker actions:

- `docker/setup-buildx-action` for BuildKit support.
- `docker/login-action` for Docker Hub authentication.
- `docker/metadata-action` for consistent tag generation.
- `docker/build-push-action` for building and pushing images.

The backend build context is `./backend`; the frontend build context is `./frontend`.

## Verification

Validation should include:

- YAML syntax check for the workflow file.
- Confirming both image references resolve from the expected Docker Hub username secret.
- Confirming build contexts and Dockerfile paths match the repository layout.
