"""Database storage-health API — read-only, authenticated.

Exposes a safe operational snapshot of the repository's SQLite database
(journal mode, page size, page/freelist counts, derived used/free space and
WAL file size). No filesystem/database paths, connection URLs, table contents
or secrets are exposed, and no PRAGMA is mutated — every probe is read-only.

The route depends on ``get_db``/Session (not the module-level engine) so
FastAPI dependency overrides are reliable in tests, and uses the existing
``snapshot_from_session`` helper.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import DatabaseHealthSnapshot
from app.services.database_health_service import snapshot_from_session

router = APIRouter(
    prefix="/api/database-health",
    tags=["database-health"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=DatabaseHealthSnapshot)
def get_database_health(db: Session = Depends(get_db)) -> DatabaseHealthSnapshot:
    """Read-only SQLite storage-health snapshot (authenticated)."""
    return snapshot_from_session(db)