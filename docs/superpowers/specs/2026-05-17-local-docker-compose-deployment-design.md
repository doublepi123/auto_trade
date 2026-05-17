# Local Docker Compose Deployment Design

Date: 2026-05-17

## Goal

Deploy the existing Auto Trade application on the current machine for local-only access.

## Approach

Use the repository's existing Docker Compose setup. This matches the current project architecture and avoids adding new deployment infrastructure.

## Runtime Layout

- Backend: FastAPI container built from `backend/Dockerfile`, exposed on `127.0.0.1:8000`.
- Frontend: Nginx container built from `frontend/Dockerfile`, exposed on `127.0.0.1:${AUTO_TRADE_FRONTEND_PORT:-8080}`.
- Database: SQLite file persisted through the local `./data` bind mount.
- Internal routing: frontend Nginx proxies `/api/` and `/ws` to the backend service inside Docker Compose.

## Configuration

Deployment uses `.env` in the repository root. Required values are:

- `AUTO_TRADE_API_KEY`: non-empty management API key required by `docker-compose.yaml`.
- `CREDENTIAL_MASTER_KEY`: local encryption key for stored broker credentials.
- Longbridge credentials via either `LONGPORT_*` or legacy `LONGBRIDGE_*` variables.
- `AUTO_TRADE_FRONTEND_PORT`: optional, defaults to `8080`.

## Execution

Run `docker compose up --build -d` from the repository root. Docker Compose builds both images, starts the backend, waits for the backend health check, then starts the frontend.

## Verification

- Check container state with `docker compose ps`.
- Check backend health through `http://localhost:8000/api/health`.
- Check the UI through `http://localhost:8080` unless `AUTO_TRADE_FRONTEND_PORT` overrides the port.

## Security Boundary

The existing Compose file binds both services to `127.0.0.1`, so this deployment is not exposed to the public network. Public access would require a separate reverse-proxy, HTTPS, and access-control design.

## Rollback

Stop the local deployment with `docker compose down`. The SQLite database remains in `./data` unless manually removed.
