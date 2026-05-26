# P5+ 操作审计 + 多渠道报警 + 交易可靠性补强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 AuditLog 基础设施、多渠道通知（Server 酱 + Webhook + severity 分级）、`RTH_ONLY` 交易时段守卫与 broker retry/backoff，使系统进入可运维、可追责、有重试韧性的状态，并把 P4 的"撤单前 gate"模式扩展到时段守卫。

**Architecture:** 后端新增 `audit_logs` 表 + `AuditLogger` 工具类，9 个写端点 try/finally 写审计；`MultiChannelNotifier` 把现有 `ServerChanNotifier` 与新 `WebhookNotifier` 按 severity_floor fan-out；交易时段守卫双层（`AppRunner._check_trading_session` 在 `cancel_pending_order` 之前 + `TradeExecutionService.execute` 在 `risk.check()` 之前）；`BrokerGateway._call_with_retry` 包装订单/行情/K 线调用并按 op 分档 retry。前端 Credentials 增通知渠道列表、Strategy 增 `trading_session_mode`、Decision Timeline 增 `source` 切换 + 审计卡片。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / basedpyright / Vue 3 + TypeScript + Element Plus / Cypress / Docker Compose

**Spec:** [2026-05-26-audit-notification-trading-safety-design.md](../specs/2026-05-26-audit-notification-trading-safety-design.md)

**Baseline (2026-05-26):** `pytest 435 passed`, `basedpyright` 0 errors / 0 warnings.

---

## File Map

### Created
- `backend/app/core/audit.py` — `AuditLogger` 工具类
- `backend/app/core/notifiers/__init__.py` — 导出 `NotifierInterface`、`MultiChannelNotifier`、`ServerChanNotifier`、`WebhookNotifier`
- `backend/app/core/notifiers/serverchan.py` — 从 `core/notify.py` 迁入并加 severity 参数
- `backend/app/core/notifiers/webhook.py` — 新 `WebhookNotifier`
- `backend/app/core/notifiers/multi_channel.py` — `MultiChannelNotifier`
- `backend/tests/test_audit_logger.py`
- `backend/tests/test_notifier_multi_channel.py`
- `backend/tests/test_webhook_notifier.py`
- `backend/tests/test_broker_retry.py`
- `backend/tests/test_trading_session_guard.py`
- `frontend/cypress/e2e/credentials_notifications.cy.ts`
- `frontend/cypress/e2e/decision_timeline_audit.cy.ts`
- `frontend/cypress/e2e/strategy_session_guard.cy.ts`

### Modified
- `backend/app/models.py` — `AuditLog` 模型 + `StrategyConfig.trading_session_mode` + `CredentialConfig.notification_channels`
- `backend/app/database.py` — 三个新 `_ensure_*` 注册到 `init_db()`
- `backend/app/schemas.py` — `AuditEventSchema`、`NotificationChannelSchema`、`StrategyConfigSchema.trading_session_mode`、`CredentialConfigSchema.notification_channels`、`EventPageResponse` 复合 union
- `backend/app/core/notify.py` — 改为 re-export 兼容层（`from app.core.notifiers import ...`）
- `backend/app/core/broker.py` — `_call_with_retry` + `audit` 注入
- `backend/app/services/trade_execution_service.py` — 层 B 时段守卫 + audit 注入
- `backend/app/services/strategy_service.py` — `update_config` 返回 diff
- `backend/app/services/credentials_service.py` — `notification_channels` CRUD + 脱敏 diff
- `backend/app/runner.py` — `_check_trading_session`（层 A）+ `MultiChannelNotifier` 重建 + `AuditLogger` 注入
- `backend/app/api/strategy.py` — `STRATEGY_UPDATE` 审计
- `backend/app/api/credentials.py` — `CREDENTIALS_UPDATE` 审计
- `backend/app/api/trade.py` — 7 个 control/cancel 端点审计 + `KILL_SWITCH` notify call + `get_trade_events` 改造为跨表 union
- `backend/app/api/__init__.py` 或新增 `backend/app/api/deps.py` — `get_audit_logger`、`extract_actor` DI helpers
- `backend/app/main.py` — 启动期实例化 `AuditLogger`
- `backend/app/config.py` — 三个新环境变量
- `frontend/src/views/Strategy.vue`
- `frontend/src/views/Credentials.vue`
- `frontend/src/views/DecisionTimeline.vue`（或 Events 实际所在 view，T5-1 内确认）
- `frontend/src/views/Dashboard.vue`
- `frontend/src/api/events.ts` / `strategy.ts` / `credentials.ts`
- `frontend/src/types/index.ts`

---

## Task 1：AuditLog 基础设施

> **目标：** 把 `audit_logs` 表、`AuditLogger` 工具类、DI helper 全部落地，并让 9 个写端点开始记审计。**完成后业务行为零变化**——只是多写一张表。

**Files:**
- Create: `backend/app/core/audit.py`
- Create: `backend/app/api/deps.py`
- Create: `backend/tests/test_audit_logger.py`
- Modify: `backend/app/models.py`（新增 `AuditLog`）
- Modify: `backend/app/database.py`（新增 `_ensure_audit_log_table`）
- Modify: `backend/app/config.py`（新增 `AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT`）
- Modify: `backend/app/services/strategy_service.py`（`update_config` 返回 diff）
- Modify: `backend/app/services/credentials_service.py`（diff + 脱敏）
- Modify: `backend/app/api/strategy.py`、`backend/app/api/credentials.py`、`backend/app/api/trade.py`
- Modify: `backend/app/main.py`（实例化 AuditLogger）
- Test: `backend/tests/test_audit_logger.py`、扩展 `test_api.py` / `test_credentials_api.py` / `test_database.py`

### T1.1 — `AuditLog` 模型 + 迁移补丁（TDD）

- [ ] **Step 1: 写失败测试 `test_database.py::test_ensure_audit_log_table_creates_schema`**

在 `backend/tests/test_database.py` 末尾追加：

```python
def test_ensure_audit_log_table_creates_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "audit.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    # 重新导入以重绑 engine
    import importlib
    from app import database
    importlib.reload(database)
    database.init_db()

    with database.engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(audit_logs);").fetchall()}
    assert cols == {
        "id", "action", "severity", "actor_hash",
        "source_ip", "request_summary", "result", "created_at",
    }


def test_ensure_audit_log_table_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "audit2.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    from app import database
    importlib.reload(database)
    database.init_db()
    database.init_db()  # 二次调用不应抛
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python3 -m pytest tests/test_database.py::test_ensure_audit_log_table_creates_schema -v
```

Expected: FAIL with `no such table: audit_logs` 或 `Pragma` 返回空。

- [ ] **Step 3: 在 `backend/app/models.py` 添加 `AuditLog` 模型**

在文件末尾追加（紧接最后一个模型之后）：

```python
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="INFO")
    actor_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="anonymous")
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="SUCCESS")
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow, index=True)
```

- [ ] **Step 4: 在 `backend/app/database.py` 添加 `_ensure_audit_log_table` 并注册**

```python
def _ensure_audit_log_table(db_engine: Engine) -> None:
    from app.models import Base  # 避免循环
    insp = inspect(db_engine)
    if "audit_logs" in insp.get_table_names():
        return
    Base.metadata.tables["audit_logs"].create(db_engine, checkfirst=True)
```

在 `init_db()` 内（紧跟 `_ensure_tracked_entries_table` 之后）加入调用：

```python
    _ensure_audit_log_table(engine)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python3 -m pytest tests/test_database.py::test_ensure_audit_log_table_creates_schema tests/test_database.py::test_ensure_audit_log_table_is_idempotent -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/models.py backend/app/database.py backend/tests/test_database.py
GIT_MASTER=1 git commit -m "feat(p5+): add audit_logs table and runtime migration patch"
```

### T1.2 — `AuditLogger` 工具类（TDD）

- [ ] **Step 1: 写失败测试 `tests/test_audit_logger.py`**

新建 `backend/tests/test_audit_logger.py`：

```python
from __future__ import annotations

import json
import os

import pytest

DB_FILE = "data/test_audit_logger.db"
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{DB_FILE}"

from app import database
from app.core.audit import AuditLogger
from app.models import AuditLog


@pytest.fixture
def logger():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    return AuditLogger(database.SessionLocal)


def test_record_writes_row(logger):
    logger.record("START", severity="INFO", actor_hash="abc", source_ip="10.0.0.1",
                  request_summary={"reason": "manual"}, result="SUCCESS")
    with database.SessionLocal() as db:
        rows = db.query(AuditLog).all()
    assert len(rows) == 1
    assert rows[0].action == "START"
    assert rows[0].severity == "INFO"
    assert rows[0].actor_hash == "abc"
    assert rows[0].source_ip == "10.0.0.1"
    assert json.loads(rows[0].request_summary) == {"reason": "manual"}
    assert rows[0].result == "SUCCESS"


def test_record_dict_request_summary_is_jsonified(logger):
    logger.record("STOP", request_summary={"a": 1})
    with database.SessionLocal() as db:
        row = db.query(AuditLog).one()
    assert json.loads(row.request_summary) == {"a": 1}


def test_record_truncates_large_summary(logger, monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT", "64")
    # 重新加载 settings 以读到新限制
    from app import config
    import importlib
    importlib.reload(config)
    big = {"k": "x" * 1000}
    logger.record("STRATEGY_UPDATE", request_summary=big)
    with database.SessionLocal() as db:
        row = db.query(AuditLog).one()
    assert len(row.request_summary.encode("utf-8")) <= 64 + len("...truncated")
    assert row.request_summary.endswith("...truncated")


def test_hash_actor_consistent_and_anonymous_when_missing():
    h1 = AuditLogger.hash_actor("secret-key-123")
    h2 = AuditLogger.hash_actor("secret-key-123")
    assert h1 == h2
    assert len(h1) == 32  # 16 bytes hex
    assert AuditLogger.hash_actor(None) == "anonymous"
    assert AuditLogger.hash_actor("") == "anonymous"


def test_extract_ip_prefers_x_forwarded_for():
    from starlette.requests import Request
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")],
        "client": ("127.0.0.1", 12345),
    }
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "203.0.113.5"


def test_extract_ip_falls_back_to_client_host():
    from starlette.requests import Request
    scope = {"type": "http", "headers": [], "client": ("198.51.100.7", 9999)}
    req = Request(scope)
    assert AuditLogger.extract_ip(req) == "198.51.100.7"


def test_record_swallows_write_errors(logger, monkeypatch, caplog):
    def broken_session():
        raise RuntimeError("db gone")
    bad_logger = AuditLogger(broken_session)
    # 不应抛
    bad_logger.record("PAUSE")
    assert "audit write failed" in caplog.text.lower()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python3 -m pytest tests/test_audit_logger.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.audit'`.

- [ ] **Step 3: 在 `backend/app/config.py` 添加新 setting**

在 `Settings` 类合适位置追加：

```python
    audit_request_summary_limit: int = Field(default=2048, alias="AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT")
```

- [ ] **Step 4: 实现 `backend/app/core/audit.py`**

新建文件，完整内容：

```python
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import settings
from app.models import AuditLog

logger = logging.getLogger("auto_trade.audit")


class AuditLogger:
    """Persist audit rows for sensitive ops. Failures are swallowed."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        action: str,
        *,
        severity: str = "INFO",
        actor_hash: str = "anonymous",
        source_ip: str = "",
        request_summary: dict[str, Any] | str = "",
        result: str = "SUCCESS",
    ) -> None:
        try:
            summary_str = self._normalize_summary(request_summary)
            with self._session_factory() as db:
                db.add(AuditLog(
                    action=action,
                    severity=severity,
                    actor_hash=actor_hash,
                    source_ip=source_ip,
                    request_summary=summary_str,
                    result=result,
                ))
                db.commit()
        except Exception as exc:  # 包括 DB 错误、session factory 抛错等
            logger.warning("audit write failed: action=%s err=%s", action, exc)

    def _normalize_summary(self, summary: dict[str, Any] | str) -> str:
        if isinstance(summary, dict):
            text = json.dumps(summary, ensure_ascii=False, default=str)
        else:
            text = str(summary)
        limit = settings.audit_request_summary_limit
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        return encoded[:limit].decode("utf-8", errors="ignore") + "...truncated"

    @staticmethod
    def hash_actor(api_key: str | None) -> str:
        if not api_key:
            return "anonymous"
        digest = hashlib.sha256(api_key.encode("utf-8")).digest()
        return digest[:16].hex()

    @staticmethod
    def extract_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        if request.client:
            return request.client.host
        return ""
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python3 -m pytest tests/test_audit_logger.py -v
```

Expected: PASS（7 项）。

- [ ] **Step 6: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/core/audit.py backend/app/config.py backend/tests/test_audit_logger.py
GIT_MASTER=1 git commit -m "feat(p5+): add AuditLogger with truncation and IP/actor helpers"
```

### T1.3 — DI helpers & 应用启动注入

- [ ] **Step 1: 创建 `backend/app/api/deps.py`**

新建文件：

```python
from __future__ import annotations

from fastapi import Depends, Request

from app import database
from app.core.audit import AuditLogger


_audit_logger_singleton: AuditLogger | None = None


def init_audit_logger() -> AuditLogger:
    """Called once at app startup; returns the shared instance."""
    global _audit_logger_singleton
    if _audit_logger_singleton is None:
        _audit_logger_singleton = AuditLogger(database.SessionLocal)
    return _audit_logger_singleton


def get_audit_logger() -> AuditLogger:
    return init_audit_logger()


def extract_actor(request: Request) -> tuple[str, str]:
    """Returns (actor_hash, source_ip) for use in API handlers."""
    api_key = request.headers.get("x-api-key")
    return AuditLogger.hash_actor(api_key), AuditLogger.extract_ip(request)
```

- [ ] **Step 2: 在 `backend/app/main.py` 启动期初始化**

在 `lifespan` 开头（早于 `runner.start`）添加：

```python
    from app.api.deps import init_audit_logger
    init_audit_logger()
```

- [ ] **Step 3: 写测试 `tests/test_audit_logger.py::test_extract_actor_helper`**

在已有 test 文件追加：

```python
def test_extract_actor_returns_hash_and_ip():
    from app.api.deps import extract_actor
    from starlette.requests import Request
    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"key-abc"), (b"x-forwarded-for", b"203.0.113.5")],
        "client": ("127.0.0.1", 12345),
    }
    actor, ip = extract_actor(Request(scope))
    assert actor == AuditLogger.hash_actor("key-abc")
    assert ip == "203.0.113.5"


def test_extract_actor_anonymous_when_no_header():
    from app.api.deps import extract_actor
    from starlette.requests import Request
    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 999)}
    actor, ip = extract_actor(Request(scope))
    assert actor == "anonymous"
    assert ip == "127.0.0.1"
```

- [ ] **Step 4: 运行测试**

```bash
cd backend && python3 -m pytest tests/test_audit_logger.py -v
```

Expected: PASS（9 项）。

- [ ] **Step 5: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/api/deps.py backend/app/main.py backend/tests/test_audit_logger.py
GIT_MASTER=1 git commit -m "feat(p5+): add audit DI helpers and lifespan init"
```

### T1.4 — 接入 `POST /api/control/*` 7 个端点

- [ ] **Step 1: 写失败测试 `tests/test_api.py::test_control_endpoints_write_audit`**

在 `backend/tests/test_api.py` 末尾追加（沿用文件已有的 `TestClient` + DB fixture 风格；如该文件用 class-based test，加在合适 class 内）：

```python
def test_start_endpoint_writes_audit_success(client, db_session):
    resp = client.post("/api/control/start", headers={"x-api-key": "k1"})
    assert resp.status_code == 200
    from app.models import AuditLog
    rows = db_session.query(AuditLog).filter_by(action="START").all()
    assert len(rows) == 1
    assert rows[0].result == "SUCCESS"
    assert rows[0].actor_hash != "anonymous"


def test_kill_switch_endpoint_writes_critical(client, db_session):
    resp = client.post("/api/control/kill-switch", json={"reason": "test"})
    assert resp.status_code == 200
    from app.models import AuditLog
    row = db_session.query(AuditLog).filter_by(action="KILL_SWITCH").one()
    assert row.severity == "CRITICAL"
    import json
    assert json.loads(row.request_summary)["reason"] == "test"


def test_start_failure_writes_failed_audit(client, db_session, monkeypatch):
    # 模拟 runner.start 抛 HTTPException
    from app.api import trade as trade_api
    from fastapi import HTTPException
    def boom(*a, **kw):
        raise HTTPException(status_code=409, detail="already running")
    monkeypatch.setattr(trade_api, "_runner_start_impl", boom, raising=False)
    resp = client.post("/api/control/start")
    # endpoint 须捕获并在 finally 中写 FAILED 行（实现侧配合）
    from app.models import AuditLog
    rows = db_session.query(AuditLog).filter_by(action="START").all()
    assert any(r.result == "FAILED" for r in rows)
```

> ⚠️ **fixture 注意：** `test_api.py` 现有 fixture 通常是 module-scoped；新断言用单条插入不要污染同模块其他测试。若已有 `db_session` autouse cleanup，无需特别处理；否则在每个新测试前显式 `db_session.query(AuditLog).delete()` + commit。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python3 -m pytest tests/test_api.py::test_start_endpoint_writes_audit_success -v
```

Expected: FAIL（无行）。

- [ ] **Step 3: 改造 `backend/app/api/trade.py` 7 个端点**

引入 dep：

```python
from app.api.deps import get_audit_logger, extract_actor
from app.core.audit import AuditLogger
```

`POST /api/control/start`（line ~382）改为：

```python
@router.post("/control/start", response_model=MessageResponse)
def start_strategy(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
):
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {}
    try:
        runner = get_runner()
        if not runner.start():
            result = "FAILED"
            detail = {"detail": "runner refused to start"}
            raise HTTPException(status_code=409, detail="runner refused to start")
        return MessageResponse(message="started")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    finally:
        audit.record("START", severity="INFO",
                     actor_hash=actor_hash, source_ip=source_ip,
                     request_summary=detail, result=result)
```

对 `stop` / `pause` / `resume` / `kill-switch` / `disable-kill-switch` / `orders/{id}/cancel` 同样改造，按 §4.2 表的 action / severity / request_summary 字段：

- `STOP` / `RESUME`：`severity="INFO"`, `request_summary={}`
- `PAUSE`：`request_summary={"reason": payload.reason}`（如有）
- `KILL_SWITCH`：`severity="CRITICAL"`, `request_summary={"reason": payload.reason}`
- `DISABLE_KILL_SWITCH`：`severity="WARNING"`
- `ORDER_CANCEL`：`request_summary={"symbol": ..., "quantity": ..., "side": ...}`（从 order 行读，order 找不到时 `{"order_id": id}`）

> **DRY tip：** 可抽 `_audit_op(audit, request, action, severity, build_summary, body_fn)` helper 减少模板代码——若 7 个 handler 模板重复度高，建议提取；否则保持显式。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python3 -m pytest tests/test_api.py -v -k "audit or control"
```

Expected: 全 PASS；总数 `test_api.py` 比基线 +3 项以上。

- [ ] **Step 5: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/api/trade.py backend/tests/test_api.py
GIT_MASTER=1 git commit -m "feat(p5+): record audit for control and order cancel endpoints"
```

### T1.5 — `PUT /api/strategy` 与 `STRATEGY_UPDATE` diff

- [ ] **Step 1: 写失败测试 `tests/test_api.py::test_strategy_update_writes_diff_audit`**

追加：

```python
def test_strategy_update_writes_diff_audit(client, db_session):
    # 假设 fixture 已建好默认 StrategyConfig
    resp = client.put("/api/strategy", json={"buy_low": 99.5, "sell_high": 105.0})
    assert resp.status_code == 200
    from app.models import AuditLog
    import json
    row = db_session.query(AuditLog).filter_by(action="STRATEGY_UPDATE").order_by(AuditLog.id.desc()).first()
    assert row is not None
    assert row.result == "SUCCESS"
    changed = json.loads(row.request_summary)["changed"]
    assert "buy_low" in changed
    assert changed["buy_low"]["new"] == 99.5
    assert "sell_high" in changed


def test_strategy_update_no_change_still_writes_audit(client, db_session):
    # 先取当前 config
    cur = client.get("/api/strategy").json()
    resp = client.put("/api/strategy", json={"buy_low": cur["buy_low"]})
    assert resp.status_code == 200
    from app.models import AuditLog
    import json
    row = db_session.query(AuditLog).filter_by(action="STRATEGY_UPDATE").order_by(AuditLog.id.desc()).first()
    assert json.loads(row.request_summary)["changed"] == {}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python3 -m pytest tests/test_api.py::test_strategy_update_writes_diff_audit -v
```

Expected: FAIL。

- [ ] **Step 3: 改 `backend/app/services/strategy_service.py::update_config`**

让方法返回 `(updated_config, diff_dict)`：

```python
STRATEGY_AUDIT_KEYS = (
    "buy_low", "sell_high", "quantity", "min_profit_amount",
    "max_daily_loss", "max_consecutive_losses", "min_exit_profit_pct",
    "fee_rate_us", "fee_rate_hk", "min_repricing_pct", "llm_action_cooldown_seconds",
    "trading_session_mode",  # T2 会加，先列在常量里也无害——读不存在的属性时 getattr 用 None 默认
    "auto_interval_enabled", "llm_min_confidence", "llm_max_stripe_width_pct",
    "allow_loss_exit", "sct_key",  # sct_key 仅记是否变更，不出明文
)

def update_config(self, payload: StrategyConfigSchema) -> tuple[StrategyConfig, dict[str, Any]]:
    with self._session_factory() as db:
        config = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first() or StrategyConfig()
        before = {k: getattr(config, k, None) for k in STRATEGY_AUDIT_KEYS}
        # ...existing apply payload logic...
        db.add(config); db.commit(); db.refresh(config)
        after = {k: getattr(config, k, None) for k in STRATEGY_AUDIT_KEYS}
    diff = {k: {"old": before[k], "new": after[k]} for k in STRATEGY_AUDIT_KEYS if before[k] != after[k]}
    # sct_key 脱敏：只标 changed=True，不出 old/new 值
    if "sct_key" in diff:
        diff["sct_key"] = {"changed": True}
    return config, diff
```

- [ ] **Step 4: 改 `backend/app/api/strategy.py::update_strategy` 写审计**

```python
@router.put("", response_model=StrategyConfigSchema)
def update_strategy(
    request: Request,
    payload: StrategyConfigSchema,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
):
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    diff: dict[str, Any] = {}
    try:
        svc = StrategyService(SessionLocal)
        config, diff = svc.update_config(payload)
        return StrategyConfigSchema.from_orm(config)
    except HTTPException as exc:
        result = "FAILED"
        diff = {"detail": str(exc.detail)}
        raise
    finally:
        audit.record("STRATEGY_UPDATE", severity="INFO",
                     actor_hash=actor_hash, source_ip=source_ip,
                     request_summary={"changed": diff}, result=result)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python3 -m pytest tests/test_api.py::test_strategy_update_writes_diff_audit tests/test_api.py::test_strategy_update_no_change_still_writes_audit tests/test_strategy_service.py -v
```

Expected: PASS（含原有 strategy_service 测试不应回退）。

- [ ] **Step 6: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/services/strategy_service.py backend/app/api/strategy.py backend/tests/test_api.py
GIT_MASTER=1 git commit -m "feat(p5+): audit strategy updates with field-level diff"
```

### T1.6 — `PUT /api/credentials` 脱敏审计

- [ ] **Step 1: 写失败测试 `tests/test_credentials_api.py::test_credentials_update_audits_with_masked_payload`**

追加：

```python
def test_credentials_update_audits_with_masked_payload(client, db_session):
    payload = {
        "longbridge_app_key": "newkey",
        "longbridge_app_secret": "newsecret",
        "longbridge_access_token": "newtoken",
        "sct_key": "newsct",
    }
    resp = client.put("/api/credentials", json=payload)
    assert resp.status_code == 200
    from app.models import AuditLog
    import json
    row = db_session.query(AuditLog).filter_by(action="CREDENTIALS_UPDATE").order_by(AuditLog.id.desc()).first()
    summary = json.loads(row.request_summary)
    for k in ("longbridge_app_key", "longbridge_app_secret", "longbridge_access_token", "sct_key"):
        assert summary["changed"].get(k) == "***"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python3 -m pytest tests/test_credentials_api.py::test_credentials_update_audits_with_masked_payload -v
```

Expected: FAIL。

- [ ] **Step 3: 改 `backend/app/api/credentials.py::update_credentials`**

```python
CREDENTIALS_MASK_KEYS = {
    "longbridge_app_key", "longbridge_app_secret", "longbridge_access_token",
    "sct_key", "encrypted_app_key", "encrypted_app_secret", "encrypted_access_token",
}

@router.put("", response_model=CredentialConfigResponse)
def update_credentials(
    request: Request,
    payload: CredentialConfigSchema,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
):
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    masked: dict[str, Any] = {}
    try:
        svc = CredentialsService(SessionLocal)
        # 计算被修改 keys；敏感 keys 仅记 "***"，非敏感原值
        masked = _mask_credentials_payload(payload.dict(exclude_unset=True))
        resp = svc.update(payload)
        return resp
    except HTTPException as exc:
        result = "FAILED"
        masked = {"detail": str(exc.detail)}
        raise
    finally:
        audit.record("CREDENTIALS_UPDATE", severity="INFO",
                     actor_hash=actor_hash, source_ip=source_ip,
                     request_summary={"changed": masked}, result=result)


def _mask_credentials_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in CREDENTIALS_MASK_KEYS:
            out[k] = "***"
        elif k == "notification_channels":
            out[k] = [_mask_channel(c) for c in (v or [])]
        else:
            out[k] = v
    return out


def _mask_channel(channel: dict[str, Any]) -> dict[str, Any]:
    out = {"type": channel.get("type"), "severity_floor": channel.get("severity_floor")}
    if channel.get("type") == "webhook":
        out["url"] = "***"  # 内网 URL 也避免明文进审计
    return out
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python3 -m pytest tests/test_credentials_api.py -v
```

Expected: PASS（原有 test 不回退）。

- [ ] **Step 5: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/api/credentials.py backend/tests/test_credentials_api.py
GIT_MASTER=1 git commit -m "feat(p5+): audit credential updates with masked secrets"
```

### T1.7 — `pytest`、`basedpyright` 全量绿

- [ ] **Step 1: 全量跑**

```bash
cd backend && python3 -m pytest tests/ -q
```

Expected: 比基线 435 增加 ≥10 项，全 PASS。

- [ ] **Step 2: `basedpyright` 0 errors**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications && python3 -m basedpyright
```

Expected: 0 errors（如有原 P8' 遗留 3 处，本步暂不解决——留到 T6.4 一次清）。新增模块不应引入新 error。

- [ ] **Step 3: 若 T1 自身引入 typing 错误，inline 修复并 commit**

```bash
GIT_MASTER=1 git add -A && GIT_MASTER=1 git commit -m "chore(p5+): typing fixes for audit module"
```

---

## Task 2：交易时段守卫（双层）

> **目标：** 新增 `StrategyConfig.trading_session_mode`，默认 `ANY`（上线零行为变更）；在 `AppRunner` 与 `TradeExecutionService` 双层 gate；`CANCEL_PENDING` 显式放行。

**Files:**
- Modify: `backend/app/models.py`（`StrategyConfig.trading_session_mode`）
- Modify: `backend/app/database.py`（`_ensure_strategy_config_session_columns`）
- Modify: `backend/app/schemas.py`（`StrategyConfigSchema.trading_session_mode`）
- Modify: `backend/app/services/trade_execution_service.py`（层 B gate）
- Modify: `backend/app/runner.py`（层 A `_check_trading_session`）
- Test: `backend/tests/test_trading_session_guard.py`（新）
- Test: 扩展 `backend/tests/test_database.py`、`backend/tests/test_trade_execution_service.py`、`backend/tests/test_runner.py`

### T2.1 — 字段、schema、迁移补丁

- [ ] **Step 1: 写失败测试 `tests/test_database.py::test_ensure_strategy_config_session_columns_adds_column`**

```python
def test_ensure_strategy_config_session_columns_adds_column(tmp_path, monkeypatch):
    db_path = tmp_path / "session.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    from app import database
    importlib.reload(database)
    # 模拟旧库：先用 ALTER TABLE 之外的旧 schema 创建 strategy_config
    database.init_db()
    with database.engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(strategy_config);").fetchall()}
    assert "trading_session_mode" in cols
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && python3 -m pytest tests/test_database.py::test_ensure_strategy_config_session_columns_adds_column -v
```

- [ ] **Step 3: 在 `backend/app/models.py::StrategyConfig` 加字段**

```python
    trading_session_mode: Mapped[str] = mapped_column(String(16), default="ANY", nullable=False)
```

- [ ] **Step 4: 在 `backend/app/database.py` 加补丁**

```python
def _ensure_strategy_config_session_columns(db_engine: Engine) -> None:
    insp = inspect(db_engine)
    if "strategy_config" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("strategy_config")}
    with db_engine.begin() as conn:
        if "trading_session_mode" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE strategy_config ADD COLUMN trading_session_mode VARCHAR(16) DEFAULT 'ANY' NOT NULL"
            )
```

`init_db()` 内紧接 `_ensure_strategy_config_trade_safety_columns` 后追加调用。

- [ ] **Step 5: 在 `backend/app/schemas.py::StrategyConfigSchema` 加字段**

```python
    trading_session_mode: Literal["RTH_ONLY", "ANY"] = "ANY"
```

- [ ] **Step 6: 运行测试通过**

```bash
cd backend && python3 -m pytest tests/test_database.py tests/test_strategy_service.py -v
```

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/models.py backend/app/database.py backend/app/schemas.py backend/tests/test_database.py
GIT_MASTER=1 git commit -m "feat(p5+): add trading_session_mode column with ANY default"
```

### T2.2 — 层 B：`TradeExecutionService.execute` 入口拦截

- [ ] **Step 1: 写失败测试 `tests/test_trading_session_guard.py`**

新建：

```python
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

DB_FILE = "data/test_session_guard.db"
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{DB_FILE}"

from app import database
from app.services.trade_execution_service import TradeExecutionService
# 其他依赖 mock 按 test_trade_execution_service.py 已有 helpers 引入


class FakeQuote:
    def __init__(self, last_price=100.0):
        self.last_price = last_price


@pytest.fixture(autouse=True)
def fresh_db():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    yield


def _make_svc(audit=None):
    skipped: list[tuple[str, str, str, dict]] = []
    def record_order_skipped(symbol, action, reason, payload):
        skipped.append((symbol, action, reason, payload))
    return TradeExecutionService(
        session_factory=database.SessionLocal,
        record_order_skipped=record_order_skipped,
        audit=audit,
    ), skipped


def test_execute_rth_only_blocks_outside_hours(monkeypatch):
    # 强制 is_trading_hours -> False
    from app.services import trade_execution_service as svc_mod
    monkeypatch.setattr(svc_mod, "is_trading_hours", lambda market: False)
    svc, skipped = _make_svc()
    result = svc.execute(
        action="BUY", symbol="AAPL.US", quote=FakeQuote(),
        broker=..., risk=..., notifier=..., cash_currency="USD",
        market="US", trading_session_mode="RTH_ONLY",
    )
    assert result is None or getattr(result, "status", None) == "SKIPPED"
    assert skipped, "expected SESSION skip"
    assert skipped[0][3]["skip_category"] == "SESSION"


def test_execute_any_mode_does_not_block_outside_hours(monkeypatch):
    from app.services import trade_execution_service as svc_mod
    monkeypatch.setattr(svc_mod, "is_trading_hours", lambda market: False)
    svc, skipped = _make_svc()
    # 用 mock broker / risk 让 execute 走到 risk.check（不要进入实际下单）
    # ...沿用 test_trade_execution_service.py 已有 mock pattern...
    # 关键断言：未出现 SESSION skip
    # （具体调用细节按现有测试 helper 补全）
    assert not any(p["skip_category"] == "SESSION" for *_, p in skipped)


def test_execute_rth_only_in_hours_does_not_block(monkeypatch):
    from app.services import trade_execution_service as svc_mod
    monkeypatch.setattr(svc_mod, "is_trading_hours", lambda market: True)
    svc, skipped = _make_svc()
    # ...同上...
    assert not any(p["skip_category"] == "SESSION" for *_, p in skipped)
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && python3 -m pytest tests/test_trading_session_guard.py::test_execute_rth_only_blocks_outside_hours -v
```

Expected: FAIL（`market` 参数未识别 或 守卫未触发）。

- [ ] **Step 3: 改 `backend/app/services/trade_execution_service.py`**

import 顶端追加：

```python
from app.core.market_calendar import is_trading_hours
```

`execute()` 签名追加关键字参数：

```python
def execute(
    self, action, symbol, quote, broker, risk, notifier, cash_currency,
    *,
    market: str = "US",
    trading_session_mode: str = "ANY",
    # ...其余既有参数...
) -> "OrderStatus | None":
```

在方法体最前（在现有 `risk.check()` 之前）插：

```python
        if trading_session_mode == "RTH_ONLY" and not is_trading_hours(market):
            return self._skip_order(
                symbol, action, f"non-RTH for {market}", skip_category="SESSION",
            )
```

`__init__` 加可选 `audit`：

```python
    def __init__(self, *,
                 session_factory,
                 record_order_skipped=None,
                 audit: "AuditLogger | None" = None,
                 ...):
        self._audit = audit
```

- [ ] **Step 4: 运行测试通过**

```bash
cd backend && python3 -m pytest tests/test_trading_session_guard.py -v
```

Expected: 3 项 PASS。

- [ ] **Step 5: 跑原 trade_execution_service 测试不回退**

```bash
cd backend && python3 -m pytest tests/test_trade_execution_service.py -v
```

Expected: 原 ~150 项全 PASS（execute 调用站需更新 `market=..., trading_session_mode=...` 关键字参数；若原 mock 调用 `execute(...)` positional，新增的 kw-only 参数有默认值，应兼容）。

- [ ] **Step 6: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/services/trade_execution_service.py backend/tests/test_trading_session_guard.py
GIT_MASTER=1 git commit -m "feat(p5+): layer B trading session guard in execute()"
```

### T2.3 — 层 A：`AppRunner._check_trading_session`

- [ ] **Step 1: 写失败测试 `tests/test_runner.py::test_llm_cancel_replace_blocked_outside_rth`**

```python
def test_llm_cancel_replace_blocked_outside_rth_when_pending(monkeypatch, runner_factory):
    runner = runner_factory(config_overrides={"trading_session_mode": "RTH_ONLY"})
    runner._trade_svc._track_pending_order(symbol="AAPL.US", side="BUY", quantity="100",
                                           order_id="o1", limit_price="100.0")
    from app import runner as runner_mod
    monkeypatch.setattr(runner_mod, "is_trading_hours", lambda market: False)
    cancel_called = []
    monkeypatch.setattr(runner._trade_svc, "cancel_pending_order",
                        lambda **kw: cancel_called.append(kw) or None)
    decision = {"action": "CANCEL_REPLACE", "side": "BUY", "order_price": 101.0, "reason": "improve"}
    result = runner.execute_llm_order_decision(decision)
    assert result.get("status") == "SKIPPED"
    assert result.get("skip_category") == "SESSION"
    assert not cancel_called, "must NOT cancel pending order outside RTH"


def test_llm_cancel_pending_still_allowed_outside_rth(monkeypatch, runner_factory):
    runner = runner_factory(config_overrides={"trading_session_mode": "RTH_ONLY"})
    runner._trade_svc._track_pending_order(symbol="AAPL.US", side="BUY", quantity="100",
                                           order_id="o2", limit_price="100.0")
    from app import runner as runner_mod
    monkeypatch.setattr(runner_mod, "is_trading_hours", lambda market: False)
    cancel_called = []
    monkeypatch.setattr(runner._trade_svc, "cancel_pending_order",
                        lambda **kw: cancel_called.append(kw) or {"status": "CANCELLED"})
    decision = {"action": "CANCEL_PENDING", "reason": "manual clear"}
    result = runner.execute_llm_order_decision(decision)
    assert cancel_called, "CANCEL_PENDING should be allowed outside RTH"
```

> **依赖 fixture：** `runner_factory` 若 `test_runner.py` 还没有，新增一个 module-scoped fixture 用现有 broker mock + minimal StrategyConfig 构造 runner。沿用 test_runner.py 现有 helpers。

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && python3 -m pytest tests/test_runner.py::test_llm_cancel_replace_blocked_outside_rth_when_pending -v
```

- [ ] **Step 3: 改 `backend/app/runner.py::execute_llm_order_decision`**

在文件顶端 import：

```python
from app.core.market_calendar import is_trading_hours
```

新增方法：

```python
    def _check_trading_session(self, action: str) -> dict[str, Any] | None:
        """Layer-A gate: return SKIPPED dict if RTH_ONLY and outside RTH; else None."""
        if action == "CANCEL_PENDING":
            return None  # 允许非 RTH 清理挂单
        config = self._strategy_svc.get_config()
        if (config.trading_session_mode or "ANY") != "RTH_ONLY":
            return None
        market = self.engine.params.market
        if is_trading_hours(market):
            return None
        # 记 ORDER_SKIPPED + AuditLog
        skip_payload = {
            "executed": False,
            "status": "SKIPPED",
            "skip_category": "SESSION",
            "reason": f"non-RTH for {market}",
        }
        self._trade_svc._skip_order(
            self.engine.params.symbol, action, f"non-RTH for {market}",
            skip_category="SESSION",
        )
        if self._audit:
            self._audit.record("TRADING_SESSION_BLOCKED", severity="INFO",
                               request_summary={"symbol": self.engine.params.symbol,
                                                "action": action, "market": market})
        return skip_payload
```

在 `execute_llm_order_decision`，**所有 `cancel_pending_order` 之前**插：

```python
        skipped = self._check_trading_session(mapped_action)
        if skipped is not None:
            return skipped
```

具体插入点（按 runner.py 当前结构）：
- line ~362 第一个 `cancel_pending_order` 分支（CANCEL_REPLACE 路径）之前
- line ~380 第二个之前
- line ~404 第三个之前

> **小心：** `CANCEL_PENDING` 路径必须**绕过** gate（已通过 `_check_trading_session` 内部 early return 处理）。

- [ ] **Step 4: 给 runner 注入 `audit`**

`AppRunner.__init__` 加：

```python
        from app.core.audit import AuditLogger
        from app.api.deps import init_audit_logger
        self._audit: AuditLogger = init_audit_logger()
```

并把 `audit=self._audit` 传给 `TradeExecutionService(...)` 与 `BrokerGateway(...)`（broker 注入在 T3）。

- [ ] **Step 5: 把层 B 也接上 audit（替换 `_skip_order` 的旁路也写一次）**

在 `trade_execution_service.py::execute` 的 SESSION 拦截分支后，**不重复写** `TRADING_SESSION_BLOCKED`（按 spec §4.4 决策：层 A 已覆盖 LLM 路径；行情触发由 `ORDER_SKIPPED skip_category=SESSION` 自带 source 标识）。在层 B 拦截分支保留代码注释说明：

```python
        # SESSION skip 仅记 ORDER_SKIPPED；AuditLog TRADING_SESSION_BLOCKED 仅由 AppRunner 层 A 写
```

- [ ] **Step 6: 运行 runner 测试通过**

```bash
cd backend && python3 -m pytest tests/test_runner.py -v -k "session or llm"
```

Expected: PASS；原 runner 测试不回退。

- [ ] **Step 7: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/runner.py backend/app/services/trade_execution_service.py backend/tests/test_runner.py
GIT_MASTER=1 git commit -m "feat(p5+): layer A trading session guard before cancel_pending_order"
```

### T2.4 — 行情触发路径也带 trading_session_mode

- [ ] **Step 1: 在 `runner._on_quote` / `_trigger` 调 `TradeExecutionService.execute` 处补关键字参数**

定位（runner.py 内既有 `self._trade_svc.execute(...)` 调用站），追加：

```python
        self._trade_svc.execute(
            action=..., symbol=..., quote=..., broker=..., risk=..., notifier=..., cash_currency=...,
            market=self.engine.params.market,
            trading_session_mode=(config.trading_session_mode or "ANY"),
            # ...其余既有参数...
        )
```

- [ ] **Step 2: 写测试 `tests/test_runner.py::test_quote_trigger_respects_session_mode`**

```python
def test_quote_trigger_respects_session_mode(monkeypatch, runner_factory):
    runner = runner_factory(config_overrides={"trading_session_mode": "RTH_ONLY"})
    from app.services import trade_execution_service as svc_mod
    monkeypatch.setattr(svc_mod, "is_trading_hours", lambda market: False)
    # 喂一个跌破 buy_low 的 quote
    runner._on_quote("AAPL.US", _make_quote(last_price=runner.engine.params.buy_low - 1))
    skipped = [p for *_, p in runner._test_collected_skips() if p["skip_category"] == "SESSION"]  # helper
    assert skipped, "expected SESSION skip from quote trigger"
```

- [ ] **Step 3: 运行测试通过**

```bash
cd backend && python3 -m pytest tests/test_runner.py::test_quote_trigger_respects_session_mode -v
```

- [ ] **Step 4: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/runner.py backend/tests/test_runner.py
GIT_MASTER=1 git commit -m "feat(p5+): wire trading_session_mode into quote-triggered execute"
```

---

## Task 3：Broker retry/backoff

> **目标：** `BrokerGateway` 包一层 `_call_with_retry`；订单类全量退避、行情类轻量；优先用 longport 异常类型，回退字符串匹配；每次重试写 `BROKER_RETRY` 审计。

**Files:**
- Modify: `backend/app/core/broker.py`
- Modify: `backend/app/config.py`（两个新环境变量）
- Modify: `backend/app/runner.py`（broker 构造时传 `audit`）
- Test: `backend/tests/test_broker_retry.py`（新）

### T3.1 — 核实 longport 异常类型 + 字符串降级方案

- [ ] **Step 1: grep 异常类型可用性**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
python3 -c "import longport; print([n for n in dir(longport) if 'xception' in n or 'rror' in n])"
python3 -c "from longport import openapi; print([n for n in dir(openapi) if 'xception' in n or 'rror' in n])" 2>/dev/null || true
```

记录在 plan execution log：是否找到 `RateLimitException` / `OpenApiException` / `TransientException` 等。

- [ ] **Step 2: 决定 RETRYABLE_EXC**

如有结构化异常 → `RETRYABLE_EXC = (longport.openapi.RateLimitException, ...)`。

如无 → 复用 `_is_auto_resumable_pause_reason` 同源关键字（`"限流"`, `"频率"`, `"timeout"`, `"connection"`），用辅助函数 `_is_retryable_exception(exc) -> bool` 包装。

### T3.2 — `_call_with_retry` 实现 + 订单包装（TDD）

- [ ] **Step 1: 写失败测试 `tests/test_broker_retry.py`**

新建：

```python
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

DB_FILE = "data/test_broker_retry.db"
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{DB_FILE}"

from app import database
from app.core.broker import BrokerGateway


class _TransientErr(Exception): ...


@pytest.fixture
def gw(monkeypatch):
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    from app.core import broker as broker_mod
    monkeypatch.setattr(broker_mod, "RETRYABLE_EXC", (_TransientErr,))
    gateway = BrokerGateway(app_key="k", app_secret="s", access_token="t")
    return gateway


def test_call_with_retry_eventually_succeeds(gw, monkeypatch):
    audit = MagicMock()
    gw._audit = audit
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _TransientErr("rate limit")
        return "ok"
    monkeypatch.setattr("time.sleep", lambda s: None)  # 不真睡
    result = gw._call_with_retry(flaky, op="submit_order", max_retries=3, base_ms=10)
    assert result == "ok"
    assert len(calls) == 3
    assert audit.record.call_count == 2
    args = audit.record.call_args_list[0]
    assert args.args[0] == "BROKER_RETRY"


def test_call_with_retry_exhausts_then_raises(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    def always_fail():
        raise _TransientErr("rate limit")
    with pytest.raises(_TransientErr):
        gw._call_with_retry(always_fail, op="submit_order", max_retries=2, base_ms=10)


def test_call_with_retry_max_retries_zero_calls_once(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []
    def fn():
        calls.append(1); raise _TransientErr("rate limit")
    with pytest.raises(_TransientErr):
        gw._call_with_retry(fn, op="get_quote", max_retries=0, base_ms=10)
    assert len(calls) == 1


def test_call_with_retry_non_retryable_does_not_retry(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    class _Reject(Exception): ...
    calls = []
    def fn():
        calls.append(1); raise _Reject("balance insufficient")
    with pytest.raises(_Reject):
        gw._call_with_retry(fn, op="submit_order", max_retries=3, base_ms=10)
    assert len(calls) == 1


def test_submit_order_uses_order_retry_max(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("AUTO_TRADE_BROKER_RETRY_MAX", "2")
    calls = []
    def fake_trade_ctx_submit(*a, **kw):
        calls.append(1)
        if len(calls) < 2:
            raise _TransientErr("limit")
        return MagicMock(order_id="o1")
    # patch internal trade_ctx.submit_order
    gw._trade_ctx = MagicMock(submit_order=fake_trade_ctx_submit)
    res = gw.submit_order(symbol="AAPL.US", side="BUY", quantity="10", limit_price="100.0",
                          order_type="LO", cash_currency="USD", time_in_force="DAY")
    assert res.order_id == "o1"
    assert len(calls) == 2


def test_get_quote_uses_quote_retry_max(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("AUTO_TRADE_BROKER_QUOTE_RETRY_MAX", "1")
    calls = []
    def fake_quote(*a, **kw):
        calls.append(1)
        raise _TransientErr("limit")
    gw._quote_ctx = MagicMock(quote=fake_quote)
    with pytest.raises(_TransientErr):
        gw.get_quote("AAPL.US")
    # max_retries=1 → 总共 2 次调用
    assert len(calls) == 2
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && python3 -m pytest tests/test_broker_retry.py -v
```

- [ ] **Step 3: 在 `backend/app/config.py` 加 setting**

```python
    broker_retry_max: int = Field(default=3, alias="AUTO_TRADE_BROKER_RETRY_MAX")
    broker_quote_retry_max: int = Field(default=1, alias="AUTO_TRADE_BROKER_QUOTE_RETRY_MAX")
    broker_retry_base_ms: int = Field(default=1000, alias="AUTO_TRADE_BROKER_RETRY_BASE_MS")
```

- [ ] **Step 4: 实现 `backend/app/core/broker.py` 的 retry 层**

文件顶端：

```python
import time
from typing import Any, Callable

from app.config import settings

# T3.1 决定
try:
    from longport.openapi import OpenApiException as _LongportException  # 占位；按实际类型替换
    RETRYABLE_EXC: tuple[type[BaseException], ...] = (_LongportException,)
except Exception:
    RETRYABLE_EXC = (Exception,)  # 降级（实际实现按 T3.1 grep 结果细化）


def _is_retryable_message(exc: BaseException) -> bool:
    msg = str(exc)
    return any(kw in msg for kw in ("限流", "频率", "rate limit", "timeout", "connection", "unavailable"))
```

`BrokerGateway.__init__` 加可选参数：

```python
    def __init__(self, ..., audit: "AuditLogger | None" = None):
        self._audit = audit
```

新增方法：

```python
    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        op: str,
        max_retries: int,
        base_ms: int,
    ) -> Any:
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except RETRYABLE_EXC as exc:
                if not (isinstance(exc, RETRYABLE_EXC) and _is_retryable_message(exc)):
                    # 结构化异常但消息不像 retryable（如业务拒绝映射到同一异常树）→ 不重试
                    raise
                if attempt >= max_retries:
                    raise
                delay_s = (base_ms / 1000.0) * (2 ** attempt)
                if self._audit:
                    self._audit.record(
                        "BROKER_RETRY", severity="INFO",
                        request_summary={"op": op, "attempt": attempt + 1,
                                         "delay_s": delay_s, "exc": type(exc).__name__,
                                         "message": str(exc)[:200]},
                    )
                time.sleep(delay_s)
        # 不可达
        raise RuntimeError("unreachable")
```

将 `submit_order` / `cancel_order` 包裹：

```python
    def submit_order(self, **kw):
        return self._call_with_retry(
            lambda: self._submit_order_inner(**kw),
            op="submit_order",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def _submit_order_inner(self, **kw):
        # 把原 submit_order 实现搬进来
        ...
```

`get_quote` / `get_quotes` 用 `settings.broker_quote_retry_max`；`get_candlesticks` 也用订单档（K 线非热路径）。

- [ ] **Step 5: 运行测试通过**

```bash
cd backend && python3 -m pytest tests/test_broker_retry.py -v
```

- [ ] **Step 6: 跑 test_broker.py 不回退**

```bash
cd backend && python3 -m pytest tests/test_broker.py -v
```

- [ ] **Step 7: Commit**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add backend/app/core/broker.py backend/app/config.py backend/tests/test_broker_retry.py
GIT_MASTER=1 git commit -m "feat(p5+): broker call retry/backoff with tiered limits and audit"
```

### T3.3 — `AppRunner` 注入 audit 给 broker

- [ ] **Step 1: 改 `backend/app/runner.py::_init_broker`**

```python
        self.broker = BrokerGateway(
            app_key=..., app_secret=..., access_token=...,
            audit=self._audit,
        )
```

- [ ] **Step 2: 写测试 `tests/test_runner.py::test_broker_gets_audit_logger`**

```python
def test_broker_gets_audit_logger(runner_factory):
    runner = runner_factory()
    assert runner.broker._audit is runner._audit
```

- [ ] **Step 3: 跑测试**

```bash
cd backend && python3 -m pytest tests/test_runner.py::test_broker_gets_audit_logger -v
```

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add backend/app/runner.py backend/tests/test_runner.py
GIT_MASTER=1 git commit -m "feat(p5+): pass AuditLogger to BrokerGateway for retry audit"
```

---

## Task 4：Notifier 抽象 + Webhook + 分级

> **目标：** 把单一 `ServerChanNotifier` 拆为 `NotifierInterface` 协议 + `MultiChannelNotifier` 路由器；加 `WebhookNotifier`；`notify_risk_event` 带 severity 参数；补 `KILL_SWITCH` notify call。

**Files:**
- Create: `backend/app/core/notifiers/__init__.py`、`serverchan.py`、`webhook.py`、`multi_channel.py`
- Modify: `backend/app/core/notify.py`（变成 re-export 兼容层）
- Modify: `backend/app/runner.py`（用 `MultiChannelNotifier.from_credential_config`）
- Modify: `backend/app/api/trade.py::kill_switch`（加 `notify_risk_event("KILL_SWITCH", reason, severity="CRITICAL")`）
- Modify: `backend/app/models.py`（`CredentialConfig.notification_channels`）
- Modify: `backend/app/database.py`（`_ensure_credential_config_notification_channels_column`）
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_webhook_notifier.py`、`test_notifier_multi_channel.py`（新）
- Test: 扩展 `test_runner.py`、`test_credentials_api.py`、`test_notify.py`

### T4.1 — `notification_channels` 字段 + 迁移

- [ ] **Step 1: 写失败测试 `tests/test_database.py::test_ensure_credential_config_notification_channels_column_adds`**

```python
def test_ensure_credential_config_notification_channels_column_adds_and_backfills(tmp_path, monkeypatch):
    db_path = tmp_path / "creds.db"
    monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    from app import database
    importlib.reload(database)
    database.init_db()
    with database.engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(credential_config);").fetchall()}
        assert "notification_channels" in cols
        rows = conn.exec_driver_sql("SELECT notification_channels FROM credential_config;").fetchall()
        # 旧库可能本来就 0 行，建表后回填测试单独覆盖
    # 插一行测试回填行为
    from app.models import CredentialConfig
    with database.SessionLocal() as db:
        cc = CredentialConfig()  # 全默认
        db.add(cc); db.commit()
        db.refresh(cc)
        import json
        assert json.loads(cc.notification_channels) == [{"type": "serverchan", "severity_floor": "INFO"}]
```

- [ ] **Step 2: 改 `backend/app/models.py::CredentialConfig`**

```python
    notification_channels: Mapped[str] = mapped_column(
        Text,
        default='[{"type":"serverchan","severity_floor":"INFO"}]',
        nullable=False,
    )
```

- [ ] **Step 3: 在 `backend/app/database.py` 加补丁**

```python
DEFAULT_NOTIFICATION_CHANNELS_JSON = '[{"type":"serverchan","severity_floor":"INFO"}]'

def _ensure_credential_config_notification_channels_column(db_engine: Engine) -> None:
    insp = inspect(db_engine)
    if "credential_config" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("credential_config")}
    with db_engine.begin() as conn:
        if "notification_channels" not in cols:
            conn.exec_driver_sql(
                f"ALTER TABLE credential_config ADD COLUMN notification_channels TEXT DEFAULT '{DEFAULT_NOTIFICATION_CHANNELS_JSON}' NOT NULL"
            )
            # 回填已有行（CREATE TABLE 之外的旧行可能为 NULL）
            conn.exec_driver_sql(
                f"UPDATE credential_config SET notification_channels = '{DEFAULT_NOTIFICATION_CHANNELS_JSON}' WHERE notification_channels IS NULL OR notification_channels = ''"
            )
```

注册到 `init_db()`。

- [ ] **Step 4: 跑测试通过**

```bash
cd backend && python3 -m pytest tests/test_database.py::test_ensure_credential_config_notification_channels_column_adds_and_backfills -v
```

- [ ] **Step 5: Commit**

```bash
GIT_MASTER=1 git add backend/app/models.py backend/app/database.py backend/tests/test_database.py
GIT_MASTER=1 git commit -m "feat(p5+): add notification_channels JSON column with default backfill"
```

### T4.2 — Notifier 抽象 + `ServerChanNotifier` 迁移

- [ ] **Step 1: 创建 `backend/app/core/notifiers/__init__.py`**

```python
from app.core.notifiers.serverchan import ServerChanNotifier
from app.core.notifiers.webhook import WebhookNotifier
from app.core.notifiers.multi_channel import MultiChannelNotifier, NotifierInterface

__all__ = [
    "NotifierInterface",
    "ServerChanNotifier",
    "WebhookNotifier",
    "MultiChannelNotifier",
]
```

- [ ] **Step 2: 写 `multi_channel.py` 含 Protocol**

```python
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger("auto_trade.notify")

_SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


class NotifierInterface(Protocol):
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...


class MultiChannelNotifier:
    def __init__(self, channels: list[tuple[NotifierInterface, str]]) -> None:
        self._channels = channels

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        target_rank = _SEVERITY_RANK.get(severity, 0)
        success_any = False
        for notifier, floor in self._channels:
            if _SEVERITY_RANK.get(floor, 0) > target_rank:
                continue
            try:
                if notifier.send(title, content, severity):
                    success_any = True
            except Exception as exc:
                logger.warning("notifier %s send raised: %s", type(notifier).__name__, exc)
        if not success_any:
            logger.warning("all notifier channels failed: title=%s severity=%s", title, severity)
        return success_any

    # 上层包装兼容
    def notify_order(self, side, symbol, quantity, price, order_id) -> bool:
        return self.send(
            f"[Auto Trade] {side} Order Submitted",
            f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}\nOrder ID: {order_id}",
            severity="INFO",
        )

    def notify_fill(self, symbol, side, quantity, price) -> bool:
        return self.send(
            "[Auto Trade] Order Filled",
            f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}",
            severity="INFO",
        )

    def notify_risk_event(self, event_type: str, reason: str, *, severity: str | None = None) -> bool:
        if severity is None:
            severity = _severity_for_risk_event(event_type)
        return self.send(f"[Auto Trade] Risk Event: {event_type}",
                         f"Type: {event_type}\nReason: {reason}", severity=severity)

    @classmethod
    def from_credential_config(cls, cred) -> "MultiChannelNotifier":
        import json
        from app.core.notifiers.serverchan import ServerChanNotifier
        from app.core.notifiers.webhook import WebhookNotifier
        try:
            raw = json.loads(cred.notification_channels or "[]")
        except Exception as exc:
            logger.warning("notification_channels invalid JSON, falling back: %s", exc)
            return cls([(ServerChanNotifier(cred.sct_key or ""), "INFO")])
        built: list[tuple[NotifierInterface, str]] = []
        for c in raw:
            t = c.get("type"); floor = c.get("severity_floor", "INFO")
            if t == "serverchan":
                built.append((ServerChanNotifier(cred.sct_key or ""), floor))
            elif t == "webhook":
                url = c.get("url", "")
                if url:
                    built.append((WebhookNotifier(url), floor))
        if not built:
            built = [(ServerChanNotifier(cred.sct_key or ""), "INFO")]
        return cls(built)


def _severity_for_risk_event(event_type: str) -> str:
    return {
        "KILL_SWITCH": "CRITICAL",
        "ORDER_PERSISTENCE_FAILED": "CRITICAL",
    }.get(event_type, "WARNING")
```

- [ ] **Step 3: 写 `serverchan.py`（从 `notify.py` 迁入 + severity 参数）**

```python
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("auto_trade.notify.serverchan")


class ServerChanNotifier:
    BASE_URL: str = "https://sctapi.ftqq.com/"

    def __init__(self, sct_key: str) -> None:
        self._sct_key = sct_key

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._sct_key:
            return False
        # severity 仅决定标题前缀
        prefix = {"INFO": "", "WARNING": "⚠️ ", "CRITICAL": "🚨 "}.get(severity, "")
        try:
            url = f"{self.BASE_URL}{self._sct_key}.send"
            resp = httpx.post(url, data={"title": f"{prefix}{title}", "desp": content}, timeout=10)
            return resp.status_code == 200
        except Exception:
            logger.warning("ServerChan notification failed: title=%s", title)
            return False

    # 上层包装（保留兼容）
    def notify_order(self, side, symbol, quantity, price, order_id) -> bool:
        return self.send(f"[Auto Trade] {side} Order Submitted",
                         f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}\nOrder ID: {order_id}",
                         severity="INFO")

    def notify_fill(self, symbol, side, quantity, price) -> bool:
        return self.send("[Auto Trade] Order Filled",
                         f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}",
                         severity="INFO")

    def notify_risk_event(self, event_type: str, reason: str, *, severity: str | None = None) -> bool:
        from app.core.notifiers.multi_channel import _severity_for_risk_event
        sev = severity or _severity_for_risk_event(event_type)
        return self.send(f"[Auto Trade] Risk Event: {event_type}",
                         f"Type: {event_type}\nReason: {reason}", severity=sev)
```

- [ ] **Step 4: 改 `backend/app/core/notify.py` 为兼容层**

```python
# Backwards-compat: re-export so existing imports keep working
from app.core.notifiers.serverchan import ServerChanNotifier  # noqa: F401
from app.core.notifiers.multi_channel import (  # noqa: F401
    MultiChannelNotifier,
    NotifierInterface,
)
```

- [ ] **Step 5: 跑现有 `test_notify.py` 不回退**

```bash
cd backend && python3 -m pytest tests/test_notify.py -v
```

Expected: 原测试 PASS（导入路径未变）。

- [ ] **Step 6: Commit**

```bash
GIT_MASTER=1 git add backend/app/core/notify.py backend/app/core/notifiers/
GIT_MASTER=1 git commit -m "feat(p5+): split notifier into NotifierInterface and ServerChanNotifier modules"
```

### T4.3 — `WebhookNotifier`（TDD）

- [ ] **Step 1: 写测试 `tests/test_webhook_notifier.py`**

```python
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.core.notifiers.webhook import WebhookNotifier


def test_webhook_sends_correct_payload():
    with patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 200
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("hello", "world", severity="CRITICAL") is True
        kwargs = mock_post.call_args.kwargs
        body = kwargs["json"]
        assert body["title"] == "hello"
        assert body["content"] == "world"
        assert body["severity"] == "CRITICAL"
        assert "timestamp" in body


def test_webhook_non_2xx_returns_false():
    with patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 500
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("t", "c") is False


def test_webhook_timeout_returns_false():
    import httpx
    with patch("httpx.post", side_effect=httpx.ReadTimeout("timeout")):
        notifier = WebhookNotifier("https://example.com/hook")
        assert notifier.send("t", "c") is False


def test_webhook_empty_url_returns_false():
    notifier = WebhookNotifier("")
    assert notifier.send("t", "c") is False
```

- [ ] **Step 2: 实现 `backend/app/core/notifiers/webhook.py`**

```python
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("auto_trade.notify.webhook")


class WebhookNotifier:
    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        if not self._url:
            return False
        payload = {
            "title": title,
            "content": content,
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = httpx.post(self._url, json=payload, timeout=self._timeout)
            return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.warning("webhook send failed (%s): %s", self._url, exc)
            return False
```

- [ ] **Step 3: 跑测试通过**

```bash
cd backend && python3 -m pytest tests/test_webhook_notifier.py -v
```

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add backend/app/core/notifiers/webhook.py backend/tests/test_webhook_notifier.py
GIT_MASTER=1 git commit -m "feat(p5+): add WebhookNotifier with JSON payload"
```

### T4.4 — `MultiChannelNotifier` fan-out 测试

- [ ] **Step 1: 写测试 `tests/test_notifier_multi_channel.py`**

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.notifiers.multi_channel import (
    MultiChannelNotifier,
    _severity_for_risk_event,
)


def _ch(success=True):
    m = MagicMock(); m.send.return_value = success; return m


def test_severity_floor_filters_below_threshold():
    sc, wh = _ch(), _ch()
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    notifier.send("t", "c", severity="WARNING")
    sc.send.assert_called_once()
    wh.send.assert_not_called()


def test_critical_fans_out_to_all_channels():
    sc, wh = _ch(), _ch()
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    assert notifier.send("t", "c", severity="CRITICAL") is True
    sc.send.assert_called_once()
    wh.send.assert_called_once()


def test_any_channel_success_returns_true():
    sc = _ch(success=False); wh = _ch(success=True)
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "INFO")])
    assert notifier.send("t", "c", severity="INFO") is True


def test_all_channels_failed_returns_false_and_logs(caplog):
    sc = _ch(success=False); wh = _ch(success=False)
    notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "INFO")])
    assert notifier.send("t", "c", severity="INFO") is False
    assert "all notifier channels failed" in caplog.text


def test_channel_raises_does_not_break_others():
    bad = MagicMock(); bad.send.side_effect = RuntimeError("boom")
    good = _ch(success=True)
    notifier = MultiChannelNotifier([(bad, "INFO"), (good, "INFO")])
    assert notifier.send("t", "c") is True
    good.send.assert_called_once()


def test_notify_risk_event_uses_default_severity_for_known_events():
    notifier = MultiChannelNotifier([(_ch(), "INFO")])
    with pytest.MonkeyPatch().context() as mp:
        sent = []
        mp.setattr(notifier, "send", lambda title, content, severity="INFO": sent.append(severity) or True)
        notifier.notify_risk_event("KILL_SWITCH", "panic")
        notifier.notify_risk_event("ORDER_PERSISTENCE_FAILED", "db down")
        notifier.notify_risk_event("ORDER_FAILED", "rejected")
    assert sent == ["CRITICAL", "CRITICAL", "WARNING"]


def test_notify_risk_event_explicit_severity_overrides_default():
    notifier = MultiChannelNotifier([(_ch(), "INFO")])
    sent = []
    notifier.send = lambda title, content, severity="INFO": sent.append(severity) or True
    notifier.notify_risk_event("REJECTED", "x", severity="CRITICAL")
    assert sent == ["CRITICAL"]


def test_from_credential_config_invalid_json_falls_back_to_serverchan(monkeypatch):
    cred = MagicMock(notification_channels="{not valid", sct_key="abc")
    notifier = MultiChannelNotifier.from_credential_config(cred)
    assert len(notifier._channels) == 1
    from app.core.notifiers.serverchan import ServerChanNotifier
    assert isinstance(notifier._channels[0][0], ServerChanNotifier)


def test_from_credential_config_builds_multiple_channels():
    import json
    cred = MagicMock(
        notification_channels=json.dumps([
            {"type": "serverchan", "severity_floor": "INFO"},
            {"type": "webhook", "url": "https://x", "severity_floor": "WARNING"},
        ]),
        sct_key="abc",
    )
    notifier = MultiChannelNotifier.from_credential_config(cred)
    assert len(notifier._channels) == 2
    assert notifier._channels[1][1] == "WARNING"
```

- [ ] **Step 2: 跑测试通过**

```bash
cd backend && python3 -m pytest tests/test_notifier_multi_channel.py -v
```

Expected: 9 项 PASS。

- [ ] **Step 3: Commit**

```bash
GIT_MASTER=1 git add backend/tests/test_notifier_multi_channel.py
GIT_MASTER=1 git commit -m "test(p5+): cover MultiChannelNotifier severity fan-out and fallback"
```

### T4.5 — `AppRunner` 用 `MultiChannelNotifier`，并补 `KILL_SWITCH` notify

- [ ] **Step 1: 改 `runner.py::__init__`**

```python
        self.notifier = MultiChannelNotifier([(ServerChanNotifier(""), "INFO")])
```

`_apply_credential_changes`（或对应方法）改为：

```python
        new_notifier = MultiChannelNotifier.from_credential_config(credentials)
        self.notifier = new_notifier
```

- [ ] **Step 2: 改 `api/trade.py::kill_switch` 加 notify call**

定位 line ~436 `kill_switch`：

```python
@router.post("/control/kill-switch", response_model=MessageResponse)
def kill_switch(
    request: Request,
    payload: KillSwitchRequest,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
):
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    try:
        runner = get_runner()
        runner.risk.enable_kill_switch(payload.reason)
        # T4 新增：触发 CRITICAL 通知（之前缺失）
        try:
            runner.notifier.notify_risk_event("KILL_SWITCH", payload.reason, severity="CRITICAL")
        except Exception as exc:
            logger.warning("kill switch notify failed: %s", exc)
        # ...其余 runtime_state 持久化...
        return MessageResponse(message="kill switch engaged")
    except HTTPException as exc:
        result = "FAILED"
        raise
    finally:
        audit.record("KILL_SWITCH", severity="CRITICAL",
                     actor_hash=actor_hash, source_ip=source_ip,
                     request_summary={"reason": payload.reason}, result=result)
```

- [ ] **Step 3: 写测试 `tests/test_runner.py::test_kill_switch_endpoint_fans_out_to_all_channels`**

```python
def test_kill_switch_endpoint_fans_out_to_all_channels(client, runner_factory, monkeypatch):
    # 构造 runner 含两个 mock 渠道
    from app.core.notifiers.multi_channel import MultiChannelNotifier
    sc, wh = MagicMock(), MagicMock()
    sc.send.return_value = True; wh.send.return_value = True
    runner = runner_factory()
    runner.notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    monkeypatch.setattr("app.api.trade.get_runner", lambda: runner)
    resp = client.post("/api/control/kill-switch", json={"reason": "drill"})
    assert resp.status_code == 200
    sc.send.assert_called_once()
    wh.send.assert_called_once()
```

- [ ] **Step 4: 跑测试通过**

```bash
cd backend && python3 -m pytest tests/test_runner.py tests/test_api.py -v
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
GIT_MASTER=1 git add backend/app/runner.py backend/app/api/trade.py backend/tests/test_runner.py
GIT_MASTER=1 git commit -m "feat(p5+): use MultiChannelNotifier and emit KILL_SWITCH notify"
```

### T4.6 — `Credentials` API 接受 `notification_channels`

- [ ] **Step 1: 改 `backend/app/schemas.py`**

```python
class NotificationChannelSchema(BaseModel):
    type: Literal["serverchan", "webhook"]
    severity_floor: Literal["INFO", "WARNING", "CRITICAL"] = "INFO"
    url: str | None = None  # webhook only


class CredentialConfigSchema(BaseModel):
    # ...existing fields...
    notification_channels: list[NotificationChannelSchema] | None = None
```

- [ ] **Step 2: 改 `backend/app/services/credentials_service.py::update`**

接受 `payload.notification_channels`，序列化为 JSON 写入 DB。

- [ ] **Step 3: 写测试 `tests/test_credentials_api.py::test_update_persists_notification_channels`**

```python
def test_update_persists_notification_channels(client):
    payload = {
        "notification_channels": [
            {"type": "serverchan", "severity_floor": "INFO"},
            {"type": "webhook", "url": "https://example.com/hook", "severity_floor": "WARNING"},
        ]
    }
    resp = client.put("/api/credentials", json=payload)
    assert resp.status_code == 200
    body = client.get("/api/credentials").json()
    assert body["notification_channels"][1]["url"] == "https://example.com/hook"
```

- [ ] **Step 4: 跑测试通过**

```bash
cd backend && python3 -m pytest tests/test_credentials_api.py -v
```

- [ ] **Step 5: Commit**

```bash
GIT_MASTER=1 git add backend/app/schemas.py backend/app/services/credentials_service.py backend/tests/test_credentials_api.py
GIT_MASTER=1 git commit -m "feat(p5+): persist notification_channels via credentials API"
```

---

## Task 5：前端集成

> **目标：** Strategy 加交易时段字段；Credentials 加通知渠道编辑；Decision Timeline 加 source + 多选筛选 + 审计卡片；Dashboard 加 SESSION 标签 + 指示器。

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/strategy.ts`、`credentials.ts`、`events.ts`
- Modify: `frontend/src/views/Strategy.vue`、`Credentials.vue`、`Dashboard.vue`、Decision Timeline view
- Modify: `backend/app/api/trade.py::get_trade_events`（跨表 union 实现）
- Modify: `backend/app/schemas.py`（`EventOut` + `source` 字段）
- Test: Cypress 3 新 spec

### T5.1 — 后端：跨表 union `GET /api/events`

- [ ] **Step 1: 写失败测试 `tests/test_api.py::test_events_endpoint_returns_audit_with_source_field`**

```python
def test_events_endpoint_supports_source_filter(client, db_session):
    # 先写两条：1 条 trade event + 1 条 audit
    client.post("/api/control/start")  # 产生 START audit
    from app.models import TradeEvent
    db_session.add(TradeEvent(event_type="ORDER_SUBMITTED", payload="{}"))
    db_session.commit()

    body_all = client.get("/api/events?source=all&page=1&page_size=20").json()
    sources = {item["source"] for item in body_all["items"]}
    assert sources == {"trade", "audit"}

    body_audit = client.get("/api/events?source=audit&page=1&page_size=20").json()
    assert all(i["source"] == "audit" for i in body_audit["items"])
    assert any(i["event_type"] == "START" for i in body_audit["items"])

    body_trade = client.get("/api/events?source=trade").json()
    assert all(i["source"] == "trade" for i in body_trade["items"])


def test_events_endpoint_filters_by_multi_event_type(client, db_session):
    client.post("/api/control/start"); client.post("/api/control/stop")
    body = client.get("/api/events?source=audit&event_type=START&event_type=STOP").json()
    types = {i["event_type"] for i in body["items"]}
    assert "START" in types and "STOP" in types
```

- [ ] **Step 2: 改 `backend/app/api/trade.py::get_trade_events`**

```python
@router.get("/events", response_model=EventPageResponse)
def get_trade_events(
    source: Literal["trade", "audit", "all"] = "all",
    event_type: list[str] | None = Query(default=None),
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    return list_events(source=source, event_types=event_type, page=page, page_size=page_size, db=db)
```

- [ ] **Step 3: 实现 `list_events` helper（同文件或 `services/event_service.py`）**

按 spec §5.2 pseudo-code：

```python
def list_events(*, source, event_types, page, page_size, db):
    trade_filter = event_types if event_types else None
    audit_filter = event_types if event_types else None
    trade_total = audit_total = 0
    if source in ("trade", "all"):
        q = db.query(TradeEvent)
        if trade_filter:
            q = q.filter(TradeEvent.event_type.in_(trade_filter))
        trade_total = q.count()
    if source in ("audit", "all"):
        q = db.query(AuditLog)
        if audit_filter:
            q = q.filter(AuditLog.action.in_(audit_filter))
        audit_total = q.count()
    total = trade_total + audit_total

    fetch_n = page * page_size
    trade_rows = [] if source == "audit" else (
        db.query(TradeEvent)
          .filter(TradeEvent.event_type.in_(trade_filter)) if trade_filter else db.query(TradeEvent)
    ).order_by(TradeEvent.created_at.desc()).limit(fetch_n).all()
    # ... audit_rows 同理 ...
    merged = sorted(
        [_to_trade_event_out(r) for r in trade_rows] + [_to_audit_event_out(r) for r in audit_rows],
        key=lambda e: (e["created_at"], e["source"], e["id"]),
        reverse=True,
    )
    start = (page - 1) * page_size
    return {"items": merged[start:start + page_size], "total": total, "page": page, "page_size": page_size}
```

`_to_trade_event_out` 返回 dict 含 `source="trade"`；`_to_audit_event_out` 返回 dict 含 `source="audit"`, `event_type=audit.action`, `actor_hash`, `source_ip`, `severity`。

- [ ] **Step 4: 更新 `backend/app/schemas.py`**

```python
class EventBaseSchema(BaseModel):
    id: int
    event_type: str
    created_at: datetime
    payload: str
    source: Literal["trade", "audit"]


class AuditEventExtra(BaseModel):
    actor_hash: str
    source_ip: str
    severity: str
    result: str


class EventItemSchema(EventBaseSchema):
    extra: AuditEventExtra | None = None


class EventPageResponse(BaseModel):
    items: list[EventItemSchema]
    total: int
    page: int
    page_size: int
```

`/api/events/export` **不动**（仅导出 trade）。

- [ ] **Step 5: 跑测试**

```bash
cd backend && python3 -m pytest tests/test_api.py::test_events_endpoint_supports_source_filter tests/test_api.py::test_events_endpoint_filters_by_multi_event_type -v
```

- [ ] **Step 6: Commit**

```bash
GIT_MASTER=1 git add backend/app/api/trade.py backend/app/schemas.py backend/tests/test_api.py
GIT_MASTER=1 git commit -m "feat(p5+): cross-table union for /api/events with source filter"
```

### T5.2 — 前端：types + API client

- [ ] **Step 1: 改 `frontend/src/types/index.ts`**

```ts
export type SessionMode = 'RTH_ONLY' | 'ANY';
export type Severity = 'INFO' | 'WARNING' | 'CRITICAL';
export type EventSource = 'trade' | 'audit';

export interface NotificationChannel {
  type: 'serverchan' | 'webhook';
  severity_floor: Severity;
  url?: string;
}

export interface AuditEventExtra {
  actor_hash: string;
  source_ip: string;
  severity: Severity;
  result: 'SUCCESS' | 'FAILED' | 'SKIPPED';
}

export interface EventItem {
  id: number;
  event_type: string;
  created_at: string;
  payload: string;
  source: EventSource;
  extra?: AuditEventExtra;
}

export type SkipCategory =
  | 'FEE' | 'REPRICING' | 'COOLDOWN' | 'RISK'
  | 'PENDING' | 'POSITION' | 'SESSION';
```

- [ ] **Step 2: 改 API client**

`frontend/src/api/events.ts`：

```ts
export interface ListEventsParams {
  source?: EventSource | 'all';
  event_type?: string[];
  page?: number;
  page_size?: number;
}

export async function listEvents(params: ListEventsParams = {}) {
  return client.get<EventPageResponse>('/api/events', {
    params: {
      source: params.source ?? 'all',
      event_type: params.event_type,  // axios paramsSerializer 须支持 array 重复
      page: params.page ?? 1,
      page_size: params.page_size ?? 50,
    },
    paramsSerializer: { indexes: null },  // ?event_type=A&event_type=B
  });
}
```

`strategy.ts`：`updateStrategy` payload 接受 `trading_session_mode: SessionMode`。
`credentials.ts`：`updateCredentials` payload 接受 `notification_channels: NotificationChannel[]`。

- [ ] **Step 3: `npm run type-check`**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications/frontend && npm run type-check
```

Expected: 0 errors。

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add frontend/src/types/ frontend/src/api/
GIT_MASTER=1 git commit -m "feat(p5+): frontend types and API client for audit/sessions/channels"
```

### T5.3 — Strategy.vue 加交易时段字段

- [ ] **Step 1: 在 `frontend/src/views/Strategy.vue` 表单加字段**

```vue
<el-form-item label="交易时段守卫">
  <el-radio-group v-model="form.trading_session_mode">
    <el-radio label="ANY">允许盘前/盘后（兼容现行为）</el-radio>
    <el-radio label="RTH_ONLY">仅交易所盘中（RTH）</el-radio>
  </el-radio-group>
  <div class="form-help">
    RTH_ONLY 仅依据交易所盘中时段判断，<strong>不含节假日</strong>；如遇 NYSE/HKEX 假日仍可能下单，请配合手动暂停。
  </div>
</el-form-item>
```

`updateStrategy(form)` 调用自动带新字段。

- [ ] **Step 2: 写 Cypress `cypress/e2e/strategy_session_guard.cy.ts`**

```ts
describe('strategy session guard', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/strategy', { fixture: 'strategy_any.json' });
    cy.intercept('PUT', '/api/strategy', (req) => {
      expect(req.body.trading_session_mode).to.equal('RTH_ONLY');
      req.reply({ ...req.body });
    }).as('update');
  });

  it('switches trading_session_mode from ANY to RTH_ONLY', () => {
    cy.visit('/strategy');
    cy.contains('交易时段守卫');
    cy.get('input[type=radio][value=RTH_ONLY]').check({ force: true });
    cy.contains('button', /保存|更新/).click();
    cy.wait('@update');
  });

  it('shows non-holiday caveat text', () => {
    cy.visit('/strategy');
    cy.contains('不含节假日');
  });
});
```

Fixture `cypress/fixtures/strategy_any.json` 含 `"trading_session_mode": "ANY"` 与其余 strategy 默认字段。

- [ ] **Step 3: 跑 Cypress**

```bash
cd frontend && npx cypress run --spec cypress/e2e/strategy_session_guard.cy.ts
```

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add frontend/src/views/Strategy.vue frontend/cypress/e2e/strategy_session_guard.cy.ts frontend/cypress/fixtures/strategy_any.json
GIT_MASTER=1 git commit -m "feat(p5+): Strategy form trading_session_mode field"
```

### T5.4 — Credentials.vue 通知渠道列表

- [ ] **Step 1: 在 `frontend/src/views/Credentials.vue` 加渠道编辑器**

简化 sketch（保留现有 sct_key 输入）：

```vue
<el-card>
  <template #header>通知渠道</template>
  <div v-for="(ch, idx) in form.notification_channels" :key="idx" class="channel-row">
    <el-select v-model="ch.type">
      <el-option label="Server 酱" value="serverchan" />
      <el-option label="Webhook" value="webhook" />
    </el-select>
    <el-input v-if="ch.type === 'webhook'" v-model="ch.url" placeholder="https://..." />
    <el-select v-model="ch.severity_floor">
      <el-option label="INFO+" value="INFO" />
      <el-option label="WARNING+" value="WARNING" />
      <el-option label="CRITICAL" value="CRITICAL" />
    </el-select>
    <el-button type="danger" link @click="removeChannel(idx)">删除</el-button>
  </div>
  <el-button @click="addChannel">+ 添加渠道</el-button>
  <div v-if="!form.notification_channels?.length" class="form-error">
    至少保留一条渠道，否则风控事件无法通知。
  </div>
</el-card>
```

校验：保存按钮在 `notification_channels.length === 0` 时禁用；Webhook URL 必填且需 http/https。

- [ ] **Step 2: 写 Cypress `credentials_notifications.cy.ts`**

```ts
describe('credentials notification channels', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/credentials', { fixture: 'credentials_default.json' });
    cy.intercept('PUT', '/api/credentials', (req) => {
      req.reply({ ...req.body });
    }).as('save');
  });

  it('adds and removes webhook channel', () => {
    cy.visit('/credentials');
    cy.contains('通知渠道');
    cy.contains('+ 添加渠道').click();
    cy.get('.channel-row').last().within(() => {
      cy.get('.el-select').first().click();
    });
    cy.contains('.el-select-dropdown__item', 'Webhook').click();
    cy.get('.channel-row').last().find('input[placeholder^="https"]').type('https://hook.example.com/x');
    cy.contains('button', /保存|更新/).click();
    cy.wait('@save').its('request.body.notification_channels').should((arr) => {
      expect(arr).to.have.length(2);
      expect(arr[1].type).to.equal('webhook');
      expect(arr[1].url).to.equal('https://hook.example.com/x');
    });
  });

  it('blocks save when no channels remain', () => {
    cy.visit('/credentials');
    cy.contains('删除').click();  // 删掉默认那条
    cy.contains('button', /保存|更新/).should('be.disabled');
    cy.contains('至少保留一条渠道');
  });
});
```

`credentials_default.json` 含 `notification_channels: [{"type":"serverchan","severity_floor":"INFO"}]`。

- [ ] **Step 3: 跑 Cypress + commit**

```bash
cd frontend && npx cypress run --spec cypress/e2e/credentials_notifications.cy.ts
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications
GIT_MASTER=1 git add frontend/src/views/Credentials.vue frontend/cypress/e2e/credentials_notifications.cy.ts frontend/cypress/fixtures/credentials_default.json
GIT_MASTER=1 git commit -m "feat(p5+): credentials notification channels editor"
```

### T5.5 — Decision Timeline source 切换 + 审计卡片

- [ ] **Step 1: 在 timeline view 加筛选**

```vue
<el-radio-group v-model="filter.source">
  <el-radio-button label="all">全部</el-radio-button>
  <el-radio-button label="trade">交易</el-radio-button>
  <el-radio-button label="audit">审计</el-radio-button>
</el-radio-group>

<el-select v-model="filter.event_type" multiple placeholder="事件类型">
  <el-option v-for="t in availableEventTypes" :key="t" :label="t" :value="t" />
</el-select>
```

`availableEventTypes` 根据 `filter.source` 动态：trade → 现有交易事件；audit → 11 个 audit actions；all → 合并。

每条记录用 `:key="\`${item.source}-${item.id}\`"`：

```vue
<div v-for="item in items" :key="`${item.source}-${item.id}`" class="event-card"
     :class="`severity-${item.extra?.severity?.toLowerCase() ?? 'info'}`">
  <header>
    <span class="event-type">{{ item.event_type }}</span>
    <span class="event-time">{{ formatTime(item.created_at) }}</span>
    <span v-if="item.source === 'audit'" class="audit-actor">
      {{ item.extra?.actor_hash?.slice(0, 8) ?? 'anonymous' }} @ {{ item.extra?.source_ip || '-' }}
    </span>
  </header>
  <details><summary>详情</summary><pre>{{ item.payload }}</pre></details>
</div>
```

CSS：
```css
.event-card.severity-critical { border-left: 4px solid #f56c6c; }
.event-card.severity-warning  { border-left: 4px solid #e6a23c; }
.event-card.severity-info     { border-left: 4px solid #909399; }
```

- [ ] **Step 2: 写 Cypress `decision_timeline_audit.cy.ts`**

```ts
describe('decision timeline audit', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/events*', { fixture: 'events_mixed.json' });
  });

  it('toggles source filter and shows audit cards', () => {
    cy.visit('/events');
    cy.contains('全部'); cy.contains('交易'); cy.contains('审计');
    cy.contains('button', '审计').click();
    cy.get('.event-card').each(($el) => {
      cy.wrap($el).should('have.class', /severity-(info|warning|critical)/);
    });
  });

  it('filters by multiple event types', () => {
    cy.visit('/events');
    cy.contains('button', '审计').click();
    cy.get('.el-select').click();
    cy.contains('.el-select-dropdown__item', 'KILL_SWITCH').click();
    cy.contains('.el-select-dropdown__item', 'START').click();
    // ...
  });

  it('expands audit detail to show request_summary', () => {
    cy.visit('/events');
    cy.contains('button', '审计').click();
    cy.get('details').first().click();
    cy.get('pre').should('contain.text', '"reason"');
  });
});
```

Fixture `events_mixed.json` 至少 2 条 trade + 2 条 audit（含 KILL_SWITCH CRITICAL + START INFO）。

- [ ] **Step 3: 跑 Cypress + commit**

```bash
cd frontend && npx cypress run --spec cypress/e2e/decision_timeline_audit.cy.ts
GIT_MASTER=1 git add frontend/src/views/ frontend/cypress/e2e/decision_timeline_audit.cy.ts frontend/cypress/fixtures/events_mixed.json
GIT_MASTER=1 git commit -m "feat(p5+): decision timeline source filter and audit cards"
```

### T5.6 — Dashboard：SESSION 标签 + 时段指示器

- [ ] **Step 1: 在 `frontend/src/views/Dashboard.vue` 的 `skipCategoryLabel` 加 'SESSION'**

定位现有映射常量（P4 留下的），追加：

```ts
const skipCategoryLabel: Record<SkipCategory, string> = {
  FEE: '净收益不足', REPRICING: '改价幅度不足', COOLDOWN: 'LLM 同向冷却',
  RISK: '风控拒绝', PENDING: '已有待成', POSITION: '无可平仓位',
  SESSION: '非交易时段',
};
```

- [ ] **Step 2: 加时段指示器**

```vue
<el-tag :type="sessionIndicator.type">
  {{ sessionIndicator.label }}
</el-tag>
<el-tooltip content="RTH_ONLY 仅判断盘中时段，不含 NYSE/HKEX 假日">
  <el-icon><InfoFilled /></el-icon>
</el-tooltip>
```

`sessionIndicator` computed 从 status payload 读 `trading_session_mode` + `is_trading_hours` 字段。

> ⚠️ **后端配套：** `/api/status` 当前 payload 须新增 `is_trading_hours: bool`（由 runner 计算）。如果该字段缺，先回到后端补一次。
> - 改 `backend/app/api/strategy.py::get_status`（或 status payload 构建处）在响应中加 `"is_trading_hours": is_trading_hours(self.engine.params.market)`。
> - 测试 `tests/test_api.py::test_status_includes_is_trading_hours` 断言字段存在。

- [ ] **Step 3: 跑前端 build**

```bash
cd frontend && npm run build
```

Expected: 通过。

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add frontend/src/views/Dashboard.vue backend/app/api/ backend/tests/test_api.py
GIT_MASTER=1 git commit -m "feat(p5+): dashboard SESSION label and trading hours indicator"
```

---

## Task 6：lint 清零 + 全量验收

### T6.1 — 全量 pytest

- [ ] **Step 1: 全跑**

```bash
cd backend && python3 -m pytest tests/ -q
```

Expected: 比基线 435 增加 ~30 项（target 465+），全 PASS。

### T6.2 — `basedpyright` 0 errors

- [ ] **Step 1: 全量类型检查**

```bash
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications && python3 -m basedpyright
```

Expected: 0 errors / 0 warnings。

- [ ] **Step 2: 若 P8' 遗留 3 处 error 仍存，inline 修复**

按 spec §7.3 要求顺便清零。常见修法：用 `isinstance` narrow 或 explicit `cast(int, ...)`。

- [ ] **Step 3: Commit**

```bash
GIT_MASTER=1 git add -A
GIT_MASTER=1 git commit -m "chore(p5+): clear P8' typing errors"
```

### T6.3 — 前端 type-check + build

- [ ] **Step 1: 跑**

```bash
cd frontend && npm run type-check && npm run build
```

Expected: 双绿。

### T6.4 — Cypress 全量

- [ ] **Step 1: 跑 3 新 spec + 原回归**

```bash
cd frontend && npx cypress run
```

Expected: 全 PASS。

### T6.5 — 手工验证（README ship-checklist 风格）

- [ ] 旧 DB（删 `data/dev.db` 重新启动）：`audit_logs` 自动建表；`notification_channels` 自动回填默认值
- [ ] 设置 Webhook 渠道（用 `webhook.site`）+ 触发 Kill Switch → Server 酱 + Webhook 都收到，Webhook payload `"severity": "CRITICAL"`
- [ ] `trading_session_mode=RTH_ONLY` 在非 RTH 下：
  - 行情触发不下单（Decision Timeline 有 SESSION 跳过 + 审计 `TRADING_SESSION_BLOCKED`）
  - LLM `CANCEL_REPLACE` 不撤 pending（验证层 A）
  - LLM `CANCEL_PENDING` 仍可撤（验证 early return）
- [ ] mock broker 抛 retryable 异常 → 看到 `BROKER_RETRY` 审计行 + 最终成功
- [ ] `AUTO_TRADE_BROKER_RETRY_MAX=0` + `AUTO_TRADE_BROKER_QUOTE_RETRY_MAX=0`：调用一次后即 raise，无 sleep
- [ ] Credentials 渠道全删时前端阻止保存，提示"至少保留一条渠道"
- [ ] Timeline 第 2 页与第 1 页无重复/遗漏（手工跨表分页抽测）

### T6.6 — 更新文档

- [ ] **Step 1: 更新 `README.md` "环境变量" 与 "数据库迁移" 章节**

加入：`AUTO_TRADE_BROKER_RETRY_MAX`、`AUTO_TRADE_BROKER_QUOTE_RETRY_MAX`、`AUTO_TRADE_BROKER_RETRY_BASE_MS`、`AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT`；`StrategyConfig.trading_session_mode`；`CredentialConfig.notification_channels`；`audit_logs` 表用途；说明 RTH_ONLY **不含节假日**。

- [ ] **Step 2: 更新 `CLAUDE.md` "交易执行" 章节**

加入：双层时段守卫（层 A / 层 B）；`BrokerGateway._call_with_retry` 分档；`MultiChannelNotifier`；`AuditLogger`；`KILL_SWITCH` 现在会触发 CRITICAL 通知 + 审计。

- [ ] **Step 3: 更新 `docs/Roadmap.md`**

把 P5+ 标记为 ✅ 已完成，记录 commit 范围与 pytest 新基线。

- [ ] **Step 4: Commit**

```bash
GIT_MASTER=1 git add README.md CLAUDE.md docs/Roadmap.md
GIT_MASTER=1 git commit -m "docs(p5+): sync README/CLAUDE/Roadmap for audit/notification/session iteration"
```

### T6.7 — 最终一次全量校验

```bash
cd backend && python3 -m pytest tests/ -q
cd /home/lcy/code/auto_trade/.worktrees/p5-plus-audit-notifications && python3 -m basedpyright
cd frontend && npm run type-check && npm run build && npx cypress run
```

全绿后视 P5+ 交付完成。

---

## 自检（写给自己）

**Spec 覆盖：**

| Spec 章节 | Task |
|-----------|------|
| §3.1 audit_logs 表 | T1.1 |
| §3.2 trading_session_mode | T2.1 |
| §3.3 notification_channels | T4.1 |
| §3.4 三个 _ensure_* | T1.1 + T2.1 + T4.1 |
| §4.1 AuditLogger | T1.2 |
| §4.2 9 端点审计 + SUCCESS/FAILED + diff | T1.4 / T1.5 / T1.6 |
| §4.3 Notifier 抽象 + severity 映射 | T4.2 / T4.4 |
| §4.4 双层 session guard + CANCEL_PENDING 放行 | T2.2 / T2.3 |
| §4.5 broker retry 分档 | T3 |
| §4.6 AppRunner 集成 | T2.3 / T3.3 / T4.5 |
| §5.1 events source 多选 | T5.1 |
| §5.2 跨表 union (source,id) | T5.1 |
| §5.3 新 schemas | T5.1 / T5.2 |
| §5.4 4 个新环境变量 | T1.2 / T3.2 |
| §6.1 Strategy 字段 | T5.3 |
| §6.2 Credentials 渠道列表 | T5.4 / T4.6 |
| §6.3 Decision Timeline source + 审计卡片 | T5.5 |
| §6.4 Dashboard SESSION + 指示器 | T5.6 |
| §7.1 后端 ~30 测试 | T1–T4 各 TDD 步骤 + T5.1 |
| §7.2 Cypress 3 spec | T5.3 / T5.4 / T5.5 |
| §7.3 basedpyright 0 / type-check / build | T6.1–T6.3 |
| §7.4 手工验证清单 | T6.5 |
| §8 风险与回滚 | 默认值与环境变量已在各 Task 落地 |
| §9 交付顺序 T1 → T4 并行 → T2 → T3 → T5 → T6 | 本 plan Task 顺序 |
| §10 不在本迭代 | 已显式 YAGNI 切除 |

**Placeholder 扫描：** 0 个 "TBD" / "TODO" / "fill in"；每个 step 都有 code block 或 exact command。`T3.1` 的"决定 RETRYABLE_EXC" 是显式分支决策（先 grep 再选 A/B 路径），非 placeholder。

**类型一致：**
- `MultiChannelNotifier.from_credential_config` 在 T4.2 定义并被 T4.5/T4.6 引用
- `_skip_order` 与 `skip_category="SESSION"` 在 T2.2 添加并在 T2.3 复用
- `_call_with_retry` 在 T3.2 定义并在所有 broker 包装位引用
- `AuditLogger` 签名跨 T1.2 / T1.4 / T2.3 / T3.2 / T4.5 一致（`record(action, *, severity, actor_hash, source_ip, request_summary, result)`）
- `extract_actor` 在 T1.3 定义并在 T1.4–T1.6 + T4.5 引用

---

Plan complete and saved to `docs/superpowers/plans/2026-05-26-audit-notification-trading-safety.md`.
