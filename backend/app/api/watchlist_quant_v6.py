from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import (
    WatchlistQuantV6ArtifactResponse,
    WatchlistQuantV6BindingPage,
    WatchlistQuantV6MemberPage,
    WatchlistQuantV6PublicationDetail,
    WatchlistQuantV6PublicationPage,
)
from app.services.watchlist_quant_v6_reader_service import (
    QuantV6ReadCursorError,
    QuantV6ReadIntegrityError,
    QuantV6ReadNotFoundError,
    WatchlistQuantV6ReaderService,
)


router = APIRouter(
    prefix="/api/watchlist/quant-v6",
    tags=["watchlist-quant-v6"],
    dependencies=[Depends(require_api_key())],
)
logger = logging.getLogger("auto_trade.watchlist_quant_v6_reader")

ResponseT = TypeVar("ResponseT")


def _reject_offset_pagination(
    legacy_page: str | None,
    legacy_page_size: str | None,
) -> None:
    if legacy_page is not None or legacy_page_size is not None:
        raise HTTPException(
            status_code=422,
            detail="offset pagination is not supported",
        )


def _read(call: Callable[[], ResponseT]) -> ResponseT:
    try:
        return call()
    except QuantV6ReadCursorError as exc:
        logger.info("quant-v6 pagination cursor was rejected")
        raise HTTPException(
            status_code=422,
            detail="invalid quant-v6 pagination cursor",
        ) from exc
    except QuantV6ReadNotFoundError as exc:
        logger.info("quant-v6 persisted object was not found")
        raise HTTPException(
            status_code=404,
            detail="persisted quant-v6 object not found",
        ) from exc
    except QuantV6ReadIntegrityError as exc:
        logger.error("quant-v6 persisted integrity failure: %s", exc)
        raise HTTPException(
            status_code=409,
            detail="persisted quant-v6 evidence failed integrity validation",
        ) from exc
    except (TypeError, ValueError, OverflowError) as exc:
        logger.error(
            "quant-v6 persisted value conversion failed: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="persisted quant-v6 evidence failed integrity validation",
        ) from exc
    except SQLAlchemyError as exc:
        logger.error(
            "quant-v6 persisted read is unavailable: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="persisted quant-v6 evidence is temporarily unavailable",
        ) from exc


@router.get(
    "/publications",
    response_model=WatchlistQuantV6PublicationPage,
)
def list_quant_v6_publications(
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None),
    market: Literal["US", "HK"] | None = Query(default=None),
    legacy_page: str | None = Query(
        default=None,
        alias="page",
        include_in_schema=False,
    ),
    legacy_page_size: str | None = Query(
        default=None,
        alias="page_size",
        include_in_schema=False,
    ),
    db: Session = Depends(get_db),
) -> WatchlistQuantV6PublicationPage:
    _reject_offset_pagination(legacy_page, legacy_page_size)
    return _read(lambda: WatchlistQuantV6ReaderService(db).list_publications(
        limit=limit,
        cursor=cursor,
        market=market,
    ))


@router.get(
    "/publications/{publication_id}",
    response_model=WatchlistQuantV6PublicationDetail,
)
def get_quant_v6_publication(
    publication_id: int = Path(ge=1, le=9_223_372_036_854_775_807),
    db: Session = Depends(get_db),
) -> WatchlistQuantV6PublicationDetail:
    return _read(
        lambda: WatchlistQuantV6ReaderService(db).get_publication(
            publication_id
        )
    )


@router.get(
    "/publications/{publication_id}/members",
    response_model=WatchlistQuantV6MemberPage,
)
def list_quant_v6_members(
    publication_id: int = Path(ge=1, le=9_223_372_036_854_775_807),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    legacy_page: str | None = Query(
        default=None,
        alias="page",
        include_in_schema=False,
    ),
    legacy_page_size: str | None = Query(
        default=None,
        alias="page_size",
        include_in_schema=False,
    ),
    db: Session = Depends(get_db),
) -> WatchlistQuantV6MemberPage:
    _reject_offset_pagination(legacy_page, legacy_page_size)
    return _read(lambda: WatchlistQuantV6ReaderService(db).list_members(
        publication_id,
        limit=limit,
        cursor=cursor,
    ))


@router.get(
    "/publications/{publication_id}/bindings",
    response_model=WatchlistQuantV6BindingPage,
)
def list_quant_v6_bindings(
    publication_id: int = Path(ge=1, le=9_223_372_036_854_775_807),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    member_ordinal: int | None = Query(default=None, ge=0, le=999),
    role: Literal["ASSESSMENT", "SESSION_INPUT", "EVENT"] | None = Query(
        default=None,
    ),
    session_date: date | None = Query(default=None),
    legacy_page: str | None = Query(
        default=None,
        alias="page",
        include_in_schema=False,
    ),
    legacy_page_size: str | None = Query(
        default=None,
        alias="page_size",
        include_in_schema=False,
    ),
    db: Session = Depends(get_db),
) -> WatchlistQuantV6BindingPage:
    _reject_offset_pagination(legacy_page, legacy_page_size)
    return _read(lambda: WatchlistQuantV6ReaderService(db).list_bindings(
        publication_id,
        limit=limit,
        cursor=cursor,
        member_ordinal=member_ordinal,
        role=role,
        session_date=session_date,
    ))


@router.get(
    "/publications/{publication_id}/artifacts/{digest_sha256}",
    response_model=WatchlistQuantV6ArtifactResponse,
)
def get_quant_v6_artifact(
    publication_id: int = Path(ge=1, le=9_223_372_036_854_775_807),
    digest_sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> WatchlistQuantV6ArtifactResponse:
    return _read(lambda: WatchlistQuantV6ReaderService(db).get_artifact(
        publication_id,
        digest_sha256,
    ))


__all__ = ["router"]
