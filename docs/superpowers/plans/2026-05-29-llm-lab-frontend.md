# P10 LLM 优化工作台前端化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只读 `/lab` 工作台页面，把 P9 已落地但前端零暴露的能力（A/B 实验与 prompt 版本、性能追踪、扩展技术指标）呈现给用户。

**Architecture:** 后端补 3 个只读改动（performance 端点 schema 化、`GET /api/experiments` 列实验名、`GET /api/indicators` 跑现有 `DataAggregator` 零 LLM 成本）；前端新增一个 `el-tabs` 三页签页面 `Lab.vue` + `api/lab.ts` + 类型 + 路由 + 导航。不动任何写路径、LLM 决策逻辑或数据库迁移。

**Tech Stack:** 后端 FastAPI + SQLAlchemy + Pydantic + pytest（TestClient）；前端 Vue 3 + Element Plus + TypeScript + axios + Cypress。

**规格：** `docs/superpowers/specs/2026-05-29-llm-lab-frontend-design.md`

---

## 文件结构

**后端（新增 / 修改）**
- Modify `backend/app/schemas.py` — 追加 `PerformanceStats`、`PerformanceVariant`、`MacdValue`、`VolumeAnalysisSchema`、`SentimentValue`、`MultiTimeframeSchema`、`IndicatorsResponse`。
- Modify `backend/app/api/performance.py` — 三端点加 `response_model`。
- Modify `backend/app/api/experiments.py` — 加 `GET /api/experiments`（distinct 实验名）。
- Modify `backend/app/domain/experiment/ab_test_manager.py` — 加 `list_experiment_names()`。
- Create `backend/app/api/indicators.py` — `GET /api/indicators` + `get_indicator_broker` DI。
- Modify `backend/app/main.py` — 注册 `indicators_router`。
- Create `backend/tests/test_performance_api.py`、`backend/tests/test_experiments_api.py`、`backend/tests/test_indicators_api.py`。

**前端（新增 / 修改）**
- Modify `frontend/src/types/index.ts` — 追加 P10 类型。
- Create `frontend/src/api/lab.ts` — 封装所有 lab API。
- Create `frontend/src/views/Lab.vue` — 三页签工作台。
- Modify `frontend/src/router/index.ts` — 加 `/lab` 路由。
- Modify `frontend/src/App.vue` — 桌面与移动导航加入口。
- Create `frontend/cypress/e2e/lab.cy.ts` — E2E。

---

## Task 1: 后端 — performance 端点 schema 化

**Files:**
- Modify: `backend/app/schemas.py`（文件末尾追加）
- Modify: `backend/app/api/performance.py`
- Test: `backend/tests/test_performance_api.py`（新建）

- [ ] **Step 1: 追加 schema**

在 `backend/app/schemas.py` 末尾追加：

```python
class PerformanceStats(BaseModel):
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float


class PerformanceVariant(BaseModel):
    variant: str
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
```

- [ ] **Step 2: 写失败测试**

新建 `backend/tests/test_performance_api.py`：

```python
import os
os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_performance_api.db"

from app.database import engine as db_engine, SessionLocal
from app.models import Base, ExperimentResult
from app.main import app
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.commit()
    db.close()


def _seed(db, experiment="exp1", variant="A", pnl=10.0, profitable=True):
    db.add(ExperimentResult(
        experiment_name=experiment,
        variant_name=variant,
        interaction_id=None,
        order_action="SUBMIT",
        predicted_direction="up",
        actual_pnl=pnl,
        was_profitable=profitable,
    ))


class TestPerformanceApi:
    def test_stats_empty_returns_zero_structure(self, clean_db):
        resp = client.get("/api/performance/stats", params={"experiment": "none"})
        assert resp.status_code == 200
        assert resp.json() == {
            "total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        }

    def test_stats_shape(self, clean_db):
        db = SessionLocal()
        _seed(db, pnl=10.0, profitable=True)
        _seed(db, pnl=-4.0, profitable=False)
        db.commit(); db.close()
        resp = client.get("/api/performance/stats", params={"experiment": "exp1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_trades"] == 2
        assert body["win_rate"] == 0.5
        assert body["total_pnl"] == 6.0

    def test_compare_shape(self, clean_db):
        db = SessionLocal()
        _seed(db, variant="A", pnl=10.0, profitable=True)
        _seed(db, variant="B", pnl=-2.0, profitable=False)
        db.commit(); db.close()
        resp = client.get("/api/performance/compare", params={"experiment": "exp1"})
        assert resp.status_code == 200
        rows = resp.json()
        assert {r["variant"] for r in rows} == {"A", "B"}
        assert all({"variant", "total_trades", "win_rate", "total_pnl", "avg_pnl"} <= r.keys() for r in rows)

    def test_recommendations_returns_list(self, clean_db):
        resp = client.get("/api/performance/recommendations", params={"experiment": "exp1"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_stats_missing_experiment_param_422(self, clean_db):
        resp = client.get("/api/performance/stats")
        assert resp.status_code == 422
```

- [ ] **Step 3: 运行测试，确认相关用例先通过、422 用例已通过**

Run: `cd backend && python3 -m pytest tests/test_performance_api.py -v`
Expected: 全部 PASS（端点已存在；`experiment` 为必填 `Query(...)` 故缺参已是 422）。本步用于建立基线；若 `was_profitable` 字段名或 `ExperimentResult` 构造参数不符报错，按实际模型修正测试构造。

- [ ] **Step 4: 给端点加 response_model**

修改 `backend/app/api/performance.py`。在 import 段加：

```python
from app.schemas import PerformanceStats, PerformanceVariant
```

把三个端点签名改为带 `response_model`：

```python
@router.get("/stats", response_model=PerformanceStats)
def get_stats(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tracker = PerformanceTracker(db)
    return tracker.get_overall_stats(experiment)


@router.get("/compare", response_model=list[PerformanceVariant])
def compare_variants(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tracker = PerformanceTracker(db)
    return tracker.compare_variants(experiment)


@router.get("/recommendations", response_model=list[str])
def get_recommendations(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[str]:
    tracker = PerformanceTracker(db)
    return tracker.get_recommendations(experiment)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python3 -m pytest tests/test_performance_api.py -v`
Expected: PASS（schema 校验通过，形状不变）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas.py backend/app/api/performance.py backend/tests/test_performance_api.py
git commit -m "feat(performance): add response_model schemas to performance endpoints"
```

---

## Task 2: 后端 — `GET /api/experiments` 列实验名

**Files:**
- Modify: `backend/app/domain/experiment/ab_test_manager.py`
- Modify: `backend/app/api/experiments.py`
- Test: `backend/tests/test_experiments_api.py`（新建）

- [ ] **Step 1: 给 ABTestManager 加方法**

在 `backend/app/domain/experiment/ab_test_manager.py` 的 `ABTestManager` 类中追加方法（紧随 `__init__` 之后）：

```python
    def list_experiment_names(self) -> list[str]:
        rows = (
            self.db.query(ExperimentResult.experiment_name)
            .distinct()
            .order_by(ExperimentResult.experiment_name.asc())
            .all()
        )
        return [name for (name,) in rows if name]
```

确认文件顶部已 import `ExperimentResult`（若无则加 `from app.models import ExperimentResult, PromptVersion` 中补上 `ExperimentResult`）。

- [ ] **Step 2: 写失败测试**

新建 `backend/tests/test_experiments_api.py`：

```python
import os
os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_experiments_api.db"

from app.database import engine as db_engine, SessionLocal
from app.models import Base, ExperimentResult, PromptVersion
from app.main import app
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.query(PromptVersion).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.query(PromptVersion).delete()
    db.commit()
    db.close()


class TestExperimentsApi:
    def test_list_experiment_names_empty(self, clean_db):
        resp = client.get("/api/experiments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_experiment_names_distinct(self, clean_db):
        db = SessionLocal()
        for name in ["exp_a", "exp_a", "exp_b"]:
            db.add(ExperimentResult(
                experiment_name=name, variant_name="A", interaction_id=None,
                order_action="SUBMIT", predicted_direction="up",
                actual_pnl=1.0, was_profitable=True,
            ))
        db.commit(); db.close()
        resp = client.get("/api/experiments")
        assert resp.status_code == 200
        assert sorted(resp.json()) == ["exp_a", "exp_b"]

    def test_version_crud_and_activate(self, clean_db):
        created = client.post("/api/experiments/versions", json={
            "name": "baseline", "version": "v1", "description": "d", "template": "TPL",
        })
        assert created.status_code == 200
        vid = created.json()["id"]
        listed = client.get("/api/experiments/versions")
        assert listed.status_code == 200
        assert any(v["id"] == vid for v in listed.json())
        act = client.post(f"/api/experiments/versions/{vid}/activate")
        assert act.status_code == 200
        active = client.get("/api/experiments/versions/active")
        assert active.status_code == 200
        assert active.json()["id"] == vid
```

- [ ] **Step 3: 运行测试，确认 `test_list_experiment_names_*` 失败**

Run: `cd backend && python3 -m pytest tests/test_experiments_api.py -v`
Expected: `test_list_experiment_names_*` FAIL（`GET /api/experiments` 404，因路由未加）；CRUD 用例应 PASS（端点已存在）。

- [ ] **Step 4: 加路由**

在 `backend/app/api/experiments.py` import 段补 `List` 已有；在 `list_versions` 之前（或文件内任意位置，紧随 `router` 定义后）加：

```python
@router.get("", response_model=List[str])
def list_experiment_names(db: Session = Depends(get_db)) -> list[str]:
    manager = ABTestManager(db)
    return manager.list_experiment_names()
```

> 注：`router` 的 `prefix="/api/experiments"`，路径写空串 `""` 即匹配 `/api/experiments`。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python3 -m pytest tests/test_experiments_api.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/app/domain/experiment/ab_test_manager.py backend/app/api/experiments.py backend/tests/test_experiments_api.py
git commit -m "feat(experiments): add GET /api/experiments to list experiment names"
```

---

## Task 3: 后端 — `GET /api/indicators`

**Files:**
- Modify: `backend/app/schemas.py`（追加指标 schema）
- Create: `backend/app/api/indicators.py`
- Modify: `backend/app/main.py`（注册 router）
- Test: `backend/tests/test_indicators_api.py`（新建）

- [ ] **Step 1: 追加指标 schema**

在 `backend/app/schemas.py` 末尾追加：

```python
class MacdValue(BaseModel):
    macd: float
    signal: float
    histogram: float


class VolumeAnalysisSchema(BaseModel):
    avg_volume: float
    volume_ratio: float
    trend: str


class SentimentValue(BaseModel):
    sentiment: str
    score: float
    description: str


class MultiTimeframeSchema(BaseModel):
    daily_trend: str
    minute_trend: str
    aligned: bool
    description: str


class IndicatorsResponse(BaseModel):
    available: bool
    symbol: str
    market: str
    atr: float | None = None
    rsi: float | None = None
    macd: MacdValue | None = None
    volume_analysis: VolumeAnalysisSchema | None = None
    sentiment: SentimentValue | None = None
    multi_timeframe: MultiTimeframeSchema | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
```

> 字段名与 `DataAggregator.fetch_market_data` 返回的键一一对应（`atr/rsi/macd/volume_analysis/sentiment/multi_timeframe/bb_upper/bb_middle/bb_lower`）。`macd`/`volume_analysis`/`sentiment`/`multi_timeframe` 在 aggregator 中是 dict / TypedDict，Pydantic 自动转模型。

- [ ] **Step 2: 写失败测试**

新建 `backend/tests/test_indicators_api.py`：

```python
import os
os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_indicators_api.db"

from app.database import engine as db_engine, SessionLocal
from app.models import Base, StrategyConfig
from app.main import app
from app.api.indicators import get_indicator_broker
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


class _FakeCandle:
    def __init__(self, close: float, volume: float = 1000.0) -> None:
        self.open = close
        self.high = close * 1.01
        self.low = close * 0.99
        self.close = close
        self.volume = volume
        self.timestamp = None


class _FakeBroker:
    """Returns enough candles to compute all indicators."""

    def get_candlesticks(self, symbol, period, count):
        n = 40
        return [_FakeCandle(100.0 + i) for i in range(n)]

    def get_quote(self, symbol):
        class Q:
            last_price = 139.0
        return Q()

    def close(self):
        pass


class _EmptyBroker:
    def get_candlesticks(self, symbol, period, count):
        return []

    def get_quote(self, symbol):
        raise RuntimeError("no quote")

    def close(self):
        pass


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_indicator_broker, None)


def _set_config(symbol="AAPL.US", market="US"):
    db = SessionLocal()
    db.add(StrategyConfig(symbol=symbol, market=market))
    db.commit()
    db.close()


class TestIndicatorsApi:
    def test_available_with_candles(self, clean_db):
        _set_config()
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["symbol"] == "AAPL.US"
        assert body["rsi"] is not None
        assert set(body["macd"].keys()) == {"macd", "signal", "histogram"}
        assert set(body["multi_timeframe"].keys()) == {
            "daily_trend", "minute_trend", "aligned", "description",
        }

    def test_unavailable_when_no_candles(self, clean_db):
        _set_config()
        app.dependency_overrides[get_indicator_broker] = lambda: _EmptyBroker()
        resp = client.get("/api/indicators", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["rsi"] is None
        assert body["macd"] is None

    def test_symbol_defaults_to_config(self, clean_db):
        _set_config(symbol="TSLA.US")
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators")
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "TSLA.US"

    def test_422_when_no_symbol_and_no_config(self, clean_db):
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators")
        assert resp.status_code == 422
```

> 注：`StrategyConfig` 构造参数若有必填项导致直接 `StrategyConfig(symbol=..., market=...)` 失败，按模型默认值补齐最少字段；目标仅是写入一行可读 `symbol`/`market`。

- [ ] **Step 3: 运行测试，确认失败**

Run: `cd backend && python3 -m pytest tests/test_indicators_api.py -v`
Expected: FAIL（`app.api.indicators` 模块不存在 / 路由未注册）。

- [ ] **Step 4: 创建 indicators 路由**

新建 `backend/app/api/indicators.py`：

```python
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.broker import BrokerGateway
from app.database import get_db
from app.models import StrategyConfig
from app.schemas import IndicatorsResponse
from app.services.data_aggregator import DataAggregator

router = APIRouter(prefix="/api", tags=["indicators"])
logger = logging.getLogger("auto_trade.indicators")


def get_indicator_broker() -> BrokerGateway | None:
    """Reuse the running app's shared broker; None if runner unavailable."""
    try:
        from app.runner import get_runner

        return get_runner().broker
    except Exception:
        logger.warning("indicator broker unavailable; falling back to None")
        return None


@router.get("/indicators", response_model=IndicatorsResponse)
def get_indicators(
    symbol: str | None = Query(default=None),
    db: Session = Depends(get_db),
    broker: BrokerGateway | None = Depends(get_indicator_broker),
) -> IndicatorsResponse:
    config = db.query(StrategyConfig).first()
    resolved_symbol = symbol or (config.symbol if config else None)
    if not resolved_symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    market = config.market if config else "US"

    aggregator = DataAggregator(broker=broker)
    data = aggregator.fetch_market_data(resolved_symbol, market)
    if not data.get("daily_candles"):
        return IndicatorsResponse(available=False, symbol=resolved_symbol, market=market)

    return IndicatorsResponse(
        available=True,
        symbol=resolved_symbol,
        market=market,
        atr=data.get("atr"),
        rsi=data.get("rsi"),
        macd=data.get("macd"),
        volume_analysis=data.get("volume_analysis"),
        sentiment=data.get("sentiment"),
        multi_timeframe=data.get("multi_timeframe"),
        bb_upper=data.get("bb_upper"),
        bb_middle=data.get("bb_middle"),
        bb_lower=data.get("bb_lower"),
    )
```

- [ ] **Step 5: 注册 router**

修改 `backend/app/main.py`：在 import 段（与其他 router import 相邻）加：

```python
from app.api.indicators import router as indicators_router
```

在 `app.include_router(performance_router)` 之后加：

```python
app.include_router(indicators_router)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python3 -m pytest tests/test_indicators_api.py -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas.py backend/app/api/indicators.py backend/app/main.py backend/tests/test_indicators_api.py
git commit -m "feat(indicators): add read-only GET /api/indicators endpoint"
```

---

## Task 4: 前端 — 类型 + API 模块 + 路由 + 导航 + Lab.vue 骨架

**Files:**
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/src/api/lab.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`
- Create: `frontend/src/views/Lab.vue`（骨架）

- [ ] **Step 1: 追加类型**

在 `frontend/src/types/index.ts` 末尾追加：

```typescript
export interface PromptVersion {
  id: number
  name: string
  version: string
  description: string
  template: string
  is_active: boolean
  created_at: string
}

export interface PromptVersionCreate {
  name: string
  version: string
  description?: string
  template: string
}

export interface ExperimentSummary {
  variant_name: string
  total_count: number
  profitable_count: number
  avg_pnl: number
  win_rate: number
}

export interface PerformanceStats {
  total_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export interface PerformanceVariant {
  variant: string
  total_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export interface MacdValue {
  macd: number
  signal: number
  histogram: number
}

export interface VolumeAnalysis {
  avg_volume: number
  volume_ratio: number
  trend: string
}

export interface SentimentValue {
  sentiment: string
  score: number
  description: string
}

export interface MultiTimeframe {
  daily_trend: string
  minute_trend: string
  aligned: boolean
  description: string
}

export interface IndicatorsResponse {
  available: boolean
  symbol: string
  market: string
  atr: number | null
  rsi: number | null
  macd: MacdValue | null
  volume_analysis: VolumeAnalysis | null
  sentiment: SentimentValue | null
  multi_timeframe: MultiTimeframe | null
  bb_upper: number | null
  bb_middle: number | null
  bb_lower: number | null
}
```

- [ ] **Step 2: 创建 API 模块**

新建 `frontend/src/api/lab.ts`：

```typescript
import { api } from './client'
import type {
  PromptVersion,
  PromptVersionCreate,
  ExperimentSummary,
  PerformanceStats,
  PerformanceVariant,
  IndicatorsResponse,
} from '../types'

export async function listPromptVersions(): Promise<PromptVersion[]> {
  const resp = await api.get('/api/experiments/versions')
  return resp.data
}

export async function createPromptVersion(payload: PromptVersionCreate): Promise<PromptVersion> {
  const resp = await api.post('/api/experiments/versions', payload)
  return resp.data
}

export async function activatePromptVersion(id: number): Promise<void> {
  await api.post(`/api/experiments/versions/${id}/activate`)
}

export async function listExperimentNames(): Promise<string[]> {
  const resp = await api.get('/api/experiments')
  return resp.data
}

export async function getExperimentSummary(name: string): Promise<ExperimentSummary[]> {
  const resp = await api.get(`/api/experiments/${encodeURIComponent(name)}/summary`)
  return resp.data
}

export async function getPerformanceStats(experiment: string): Promise<PerformanceStats> {
  const resp = await api.get('/api/performance/stats', { params: { experiment } })
  return resp.data
}

export async function comparePerformanceVariants(experiment: string): Promise<PerformanceVariant[]> {
  const resp = await api.get('/api/performance/compare', { params: { experiment } })
  return resp.data
}

export async function getPerformanceRecommendations(experiment: string): Promise<string[]> {
  const resp = await api.get('/api/performance/recommendations', { params: { experiment } })
  return resp.data
}

export async function getIndicators(symbol?: string): Promise<IndicatorsResponse> {
  const resp = await api.get('/api/indicators', { params: symbol ? { symbol } : {} })
  return resp.data
}
```

- [ ] **Step 3: 创建 Lab.vue 骨架**

新建 `frontend/src/views/Lab.vue`（三页签空壳，后续 Task 填充内容）：

```vue
<template>
  <div class="lab-page" data-testid="lab-page">
    <h2>LLM 优化工作台</h2>
    <el-tabs v-model="activeTab" data-testid="lab-tabs">
      <el-tab-pane label="实验与版本" name="experiments">
        <div data-testid="tab-experiments">实验与版本（待填充）</div>
      </el-tab-pane>
      <el-tab-pane label="性能看板" name="performance">
        <div data-testid="tab-performance">性能看板（待填充）</div>
      </el-tab-pane>
      <el-tab-pane label="指标面板" name="indicators">
        <div data-testid="tab-indicators">指标面板（待填充）</div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const activeTab = ref('experiments')
</script>

<style scoped>
.lab-page {
  padding: 16px;
}
</style>
```

- [ ] **Step 4: 注册路由**

修改 `frontend/src/router/index.ts`：在 import 段加 `import Lab from '../views/Lab.vue'`（与其它静态 import 一致），在 `routes` 数组的 `/watchlist` 之后加 `{ path: '/lab', component: Lab },`。

- [ ] **Step 5: 加导航入口**

修改 `frontend/src/App.vue`：在桌面 `el-menu`（`data-testid="desktop-nav"`）中 `/events` 之后加 `<el-menu-item index="/lab">优化工作台</el-menu-item>`。移动 `bottom-nav` 可选不加（避免拥挤，与 Backtest/Watchlist 同处理）。

- [ ] **Step 6: 类型检查与构建**

Run: `cd frontend && npm run type-check && npm run build`
Expected: 通过，无类型错误。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/lab.ts frontend/src/views/Lab.vue frontend/src/router/index.ts frontend/src/App.vue
git commit -m "feat(lab): add Lab page scaffold, route, nav, api module and types"
```

---

## Task 5: 前端 — 三页签内容

**Files:**
- Modify: `frontend/src/views/Lab.vue`

- [ ] **Step 1: 填充 Tab 1（实验与版本管理）**

替换 `Lab.vue` 的 `<script setup>` 与三个 `el-tab-pane` 内容。先实现完整脚本逻辑：

```vue
<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listPromptVersions, createPromptVersion, activatePromptVersion,
  listExperimentNames, getExperimentSummary,
  getPerformanceStats, comparePerformanceVariants, getPerformanceRecommendations,
  getIndicators,
} from '../api/lab'
import type {
  PromptVersion, ExperimentSummary, PerformanceStats,
  PerformanceVariant, IndicatorsResponse,
} from '../types'

const activeTab = ref('experiments')

// --- Tab 1: versions & experiments ---
const versions = ref<PromptVersion[]>([])
const newVersion = reactive({ name: '', version: '', description: '', template: '' })
const creating = ref(false)
const experimentNames = ref<string[]>([])
const selectedSummaryExp = ref('')
const summary = ref<ExperimentSummary[]>([])

async function loadVersions() {
  versions.value = await listPromptVersions()
}
async function loadExperimentNames() {
  experimentNames.value = await listExperimentNames()
}
async function submitVersion() {
  if (!newVersion.name || !newVersion.version || !newVersion.template) {
    ElMessage.warning('name / version / template 必填')
    return
  }
  creating.value = true
  try {
    await createPromptVersion({ ...newVersion })
    ElMessage.success('版本已创建')
    newVersion.name = ''; newVersion.version = ''; newVersion.description = ''; newVersion.template = ''
    await loadVersions()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '创建失败')
  } finally {
    creating.value = false
  }
}
async function activate(v: PromptVersion) {
  await ElMessageBox.confirm(`确认将 "${v.name} ${v.version}" 设为激活版本？`, '确认激活')
  await activatePromptVersion(v.id)
  ElMessage.success('已激活')
  await loadVersions()
}
async function loadSummary() {
  if (!selectedSummaryExp.value) return
  summary.value = await getExperimentSummary(selectedSummaryExp.value)
}

// --- Tab 2: performance ---
const perfExp = ref('')
const stats = ref<PerformanceStats | null>(null)
const variants = ref<PerformanceVariant[]>([])
const recommendations = ref<string[]>([])

async function loadPerformance() {
  if (!perfExp.value) { stats.value = null; variants.value = []; recommendations.value = []; return }
  const [s, c, r] = await Promise.all([
    getPerformanceStats(perfExp.value),
    comparePerformanceVariants(perfExp.value),
    getPerformanceRecommendations(perfExp.value),
  ])
  stats.value = s; variants.value = c; recommendations.value = r
}

// --- Tab 3: indicators ---
const indicatorSymbol = ref('')
const indicators = ref<IndicatorsResponse | null>(null)
const indicatorsLoading = ref(false)

async function loadIndicators() {
  indicatorsLoading.value = true
  try {
    indicators.value = await getIndicators(indicatorSymbol.value || undefined)
    indicatorSymbol.value = indicators.value.symbol
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail ?? '指标加载失败')
  } finally {
    indicatorsLoading.value = false
  }
}

function pct(v: number): string { return `${(v * 100).toFixed(1)}%` }

onMounted(async () => {
  await Promise.all([loadVersions(), loadExperimentNames()])
})
</script>
```

- [ ] **Step 2: 填充三个 `el-tab-pane` 模板**

替换 `Lab.vue` 的 `<template>` 内三个 pane：

```vue
      <el-tab-pane label="实验与版本" name="experiments">
        <div data-testid="tab-experiments">
          <el-card header="Prompt 版本">
            <el-table :data="versions" data-testid="versions-table">
              <el-table-column prop="name" label="名称" />
              <el-table-column prop="version" label="版本" />
              <el-table-column prop="description" label="说明" />
              <el-table-column label="激活">
                <template #default="{ row }">
                  <el-tag v-if="row.is_active" type="success">激活中</el-tag>
                  <el-button v-else size="small" @click="activate(row)" data-testid="activate-btn">设为激活</el-button>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" label="创建时间" />
            </el-table>
          </el-card>

          <el-card header="新建版本" style="margin-top: 12px">
            <el-form label-width="90px">
              <el-form-item label="名称"><el-input v-model="newVersion.name" data-testid="v-name" /></el-form-item>
              <el-form-item label="版本号"><el-input v-model="newVersion.version" data-testid="v-version" /></el-form-item>
              <el-form-item label="说明"><el-input v-model="newVersion.description" /></el-form-item>
              <el-form-item label="模板">
                <el-input v-model="newVersion.template" type="textarea" :rows="6" data-testid="v-template" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="creating" @click="submitVersion" data-testid="create-version-btn">创建</el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card header="实验摘要" style="margin-top: 12px">
            <el-select v-model="selectedSummaryExp" placeholder="选择实验" @change="loadSummary" data-testid="summary-exp-select">
              <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
            </el-select>
            <el-table :data="summary" style="margin-top: 8px">
              <el-table-column prop="variant_name" label="变体" />
              <el-table-column prop="total_count" label="样本" />
              <el-table-column prop="profitable_count" label="盈利数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="性能看板" name="performance">
        <div data-testid="tab-performance">
          <el-select v-model="perfExp" placeholder="选择实验" @change="loadPerformance" data-testid="perf-exp-select">
            <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
          </el-select>
          <el-empty v-if="!perfExp" description="请选择实验" />
          <template v-else>
            <el-row :gutter="12" style="margin-top: 12px" data-testid="perf-stats">
              <el-col :span="6"><el-statistic title="总交易" :value="stats?.total_trades ?? 0" /></el-col>
              <el-col :span="6"><el-statistic title="胜率" :value="(stats?.win_rate ?? 0) * 100" suffix="%" /></el-col>
              <el-col :span="6"><el-statistic title="总PnL" :value="stats?.total_pnl ?? 0" /></el-col>
              <el-col :span="6"><el-statistic title="平均PnL" :value="stats?.avg_pnl ?? 0" /></el-col>
            </el-row>
            <el-table :data="variants" style="margin-top: 12px" data-testid="perf-variants">
              <el-table-column prop="variant" label="变体" />
              <el-table-column prop="total_trades" label="交易数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="total_pnl" label="总PnL" />
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
            <el-card header="优化建议" style="margin-top: 12px" data-testid="perf-recommendations">
              <ul><li v-for="(r, i) in recommendations" :key="i">{{ r }}</li></ul>
            </el-card>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="指标面板" name="indicators">
        <div data-testid="tab-indicators">
          <el-input v-model="indicatorSymbol" placeholder="标的（留空取当前策略）" style="width: 240px" data-testid="indicator-symbol" />
          <el-button type="primary" :loading="indicatorsLoading" @click="loadIndicators" data-testid="load-indicators-btn">查询</el-button>
          <span style="margin-left: 8px; color: #909399">实时快照，非历史复盘</span>

          <el-empty v-if="indicators && !indicators.available" description="行情不可用（broker 凭证缺失或限流）" data-testid="indicators-unavailable" />
          <el-row v-else-if="indicators && indicators.available" :gutter="12" style="margin-top: 12px" data-testid="indicators-grid">
            <el-col :span="8"><el-card header="RSI(14)">{{ indicators.rsi?.toFixed(2) }}</el-card></el-col>
            <el-col :span="8"><el-card header="MACD">macd {{ indicators.macd?.macd?.toFixed(3) }} / signal {{ indicators.macd?.signal?.toFixed(3) }} / hist {{ indicators.macd?.histogram?.toFixed(3) }}</el-card></el-col>
            <el-col :span="8"><el-card header="成交量">量比 {{ indicators.volume_analysis?.volume_ratio?.toFixed(2) }}（{{ indicators.volume_analysis?.trend }}）</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="市场情绪">{{ indicators.sentiment?.sentiment }}（{{ indicators.sentiment?.score?.toFixed(2) }}）<br>{{ indicators.sentiment?.description }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="多时间框架">{{ indicators.multi_timeframe?.description }}<br>对齐：{{ indicators.multi_timeframe?.aligned ? '是' : '否' }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="ATR / 布林带">ATR {{ indicators.atr?.toFixed(3) }}<br>上 {{ indicators.bb_upper?.toFixed(2) }} / 中 {{ indicators.bb_middle?.toFixed(2) }} / 下 {{ indicators.bb_lower?.toFixed(2) }}</el-card></el-col>
          </el-row>
        </div>
      </el-tab-pane>
```

- [ ] **Step 3: 类型检查与构建**

Run: `cd frontend && npm run type-check && npm run build`
Expected: 通过。若 `el-statistic` 的 `:value` 类型告警，把 `stats?.total_trades ?? 0` 包成 `Number(...)`。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/Lab.vue
git commit -m "feat(lab): implement experiments, performance and indicators tabs"
```

---

## Task 6: Cypress E2E + 文档 + 全量 lint

**Files:**
- Create: `frontend/cypress/e2e/lab.cy.ts`
- Modify: `README.md`、`docs/Roadmap.md`、`CLAUDE.md`

- [ ] **Step 1: 写 Cypress spec**

新建 `frontend/cypress/e2e/lab.cy.ts`：

```typescript
describe('LLM Lab workbench', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/experiments/versions', {
      body: [
        { id: 1, name: 'baseline', version: 'v1', description: 'd', template: 'TPL', is_active: true, created_at: '2026-05-29T00:00:00Z' },
      ],
    }).as('versions')
    cy.intercept('GET', '/api/experiments', { body: ['exp1'] }).as('expNames')
    cy.intercept('GET', '/api/experiments/exp1/summary', { body: [] }).as('summary')
    cy.intercept('GET', '/api/performance/stats*', { body: { total_trades: 3, win_rate: 0.66, total_pnl: 12, avg_pnl: 4 } }).as('stats')
    cy.intercept('GET', '/api/performance/compare*', { body: [{ variant: 'A', total_trades: 3, win_rate: 0.66, total_pnl: 12, avg_pnl: 4 }] }).as('compare')
    cy.intercept('GET', '/api/performance/recommendations*', { body: ['变体 A 表现优秀'] }).as('recs')
    cy.visit('/#/lab')
  })

  it('renders three tabs and version table', () => {
    cy.get('[data-testid="lab-tabs"]').should('exist')
    cy.wait('@versions')
    cy.get('[data-testid="versions-table"]').should('contain', 'baseline')
  })

  it('loads performance when experiment selected', () => {
    cy.contains('.el-tabs__item', '性能看板').click()
    cy.get('[data-testid="perf-exp-select"]').click()
    cy.get('.el-select-dropdown__item').contains('exp1').click()
    cy.wait(['@stats', '@compare', '@recs'])
    cy.get('[data-testid="perf-variants"]').should('contain', 'A')
    cy.get('[data-testid="perf-recommendations"]').should('contain', '表现优秀')
  })

  it('shows watermark when indicators unavailable', () => {
    cy.intercept('GET', '/api/indicators*', { body: { available: false, symbol: 'AAPL.US', market: 'US', atr: null, rsi: null, macd: null, volume_analysis: null, sentiment: null, multi_timeframe: null, bb_upper: null, bb_middle: null, bb_lower: null } }).as('ind')
    cy.contains('.el-tabs__item', '指标面板').click()
    cy.get('[data-testid="load-indicators-btn"]').click()
    cy.wait('@ind')
    cy.get('[data-testid="indicators-unavailable"]').should('exist')
  })

  it('renders indicator cards when available', () => {
    cy.intercept('GET', '/api/indicators*', { body: { available: true, symbol: 'AAPL.US', market: 'US', atr: 1.2, rsi: 55.3, macd: { macd: 0.5, signal: 0.3, histogram: 0.2 }, volume_analysis: { avg_volume: 1000, volume_ratio: 1.1, trend: 'normal' }, sentiment: { sentiment: 'bullish', score: 0.6, description: '偏多' }, multi_timeframe: { daily_trend: 'up', minute_trend: 'up', aligned: true, description: '日线趋势: up, 分钟趋势: up, 趋势一致' }, bb_upper: 110, bb_middle: 100, bb_lower: 90 } }).as('ind')
    cy.contains('.el-tabs__item', '指标面板').click()
    cy.get('[data-testid="load-indicators-btn"]').click()
    cy.wait('@ind')
    cy.get('[data-testid="indicators-grid"]').should('contain', '55.30')
  })
})
```

- [ ] **Step 2: 运行 Cypress（如本地配置可跑）**

Run: `cd frontend && npx cypress run --spec cypress/e2e/lab.cy.ts`
Expected: 4 个用例通过。若本地无 Cypress 运行环境，至少确保 spec 语法通过 `npm run type-check`（如项目对 cypress 也做 TS 检查）。

- [ ] **Step 3: 后端全量测试 + lint**

Run: `cd backend && python3 -m pytest tests/ -q && python3 -m basedpyright`
Expected: 全绿（在 P9 的 549 基线上 +约 12 项）；`basedpyright` 0 errors / 0 warnings。

- [ ] **Step 4: 前端全量检查**

Run: `cd frontend && npm run type-check && npm run build`
Expected: 通过。

- [ ] **Step 5: 更新文档**

- `README.md`：API 表新增 `GET /api/experiments`、`GET /api/indicators`、`GET /api/performance/{stats,compare,recommendations}`；新增「优化工作台 / Lab」页面说明。
- `docs/Roadmap.md`：新增「P10 LLM 优化工作台前端化 ✅」小节，记录交付摘要与验证结果（pytest 通过数、basedpyright 0/0、type-check + build 通过）。
- `CLAUDE.md`：在「API 速查」表 LLM / 实时 区域附近补三端点；在目录结构标注 `backend/app/api/indicators.py` 与 `frontend/src/views/Lab.vue`。

- [ ] **Step 6: Commit**

```bash
git add frontend/cypress/e2e/lab.cy.ts README.md docs/Roadmap.md CLAUDE.md
git commit -m "test(lab): add Cypress e2e and sync README/Roadmap/CLAUDE for P10"
```

---

## 自检结果（writing-plans self-review）

- **Spec 覆盖**：T1 性能 schema → Task 1；T1 `GET /api/experiments` → Task 2；T1 `/api/indicators` → Task 3；T2 前端骨架 → Task 4；T3/T4/T5 三页签 → Task 5；T6 测试与文档 → Task 6。规格 §4 schema、§6 错误处理（422 / available=false / 空数据 200）、§7 测试矩阵均有对应步骤。
- **Schema 修正**：规格初稿写的 `bollinger: dict` 已据 `fetch_market_data` 实际返回修正为 `bb_upper/bb_middle/bb_lower` 三个 float 字段；`multi_timeframe` 固定为 `MultiTimeframeSchema`（daily_trend/minute_trend/aligned/description）。
- **类型一致**：后端 `IndicatorsResponse` 字段 ↔ 前端 `IndicatorsResponse` 接口逐字段对应；`api/lab.ts` 函数名与 Task 5 调用处一致。
- **无占位符**：每个改代码步骤均给出完整代码与确切命令、预期输出。
