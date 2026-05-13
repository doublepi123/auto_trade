from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.credentials import router as credentials_router
from app.api.strategy import router as strategy_router
from app.api.trade import router as trade_router
from app.api.ws import router as ws_router
from app.config import settings
from app.database import init_db
from app.runner import get_runner


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    get_runner().start()
    yield
    get_runner().stop()


app = FastAPI(title="Auto Trade", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)
app.include_router(credentials_router)
app.include_router(trade_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "env": settings.env}
