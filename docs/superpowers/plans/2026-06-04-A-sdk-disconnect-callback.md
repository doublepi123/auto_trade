# A. P5' SDK disconnect 回调接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 90s 静默看门狗基础上，接 longport SDK 的 disconnect 事件回调；如 SDK 不暴露则保留看门兜底。

**Architecture:** BrokerGateway 暴露 `register_disconnect_hook(callable)`；AppRunner 注册 `_on_disconnect` 处理（审计 + 标记 `_quote_subscribed = False`）；下一 tick 由 `_run_loop` 触发重订。失败连续 ≥3 次只写 `BROKER_RETRY_EXHAUSTED` 审计，不自动 pause。

**Tech Stack:** longport SDK 0.x（Python）/ SQLAlchemy 2.0 / pytest 9 + pytest-asyncio / basedpyright / project inline-fake 测试约定

**前置阅读：**
- 母 spec `docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.1 + §5.1
- `backend/app/core/broker.py` 现有 `BrokerGateway` 实现
- `backend/app/runner.py` 现有 `_run_loop` + 90s 静默看门狗

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Modify | `backend/app/core/broker.py` | 新增 `register_disconnect_hook` + `_call_disconnect_hooks`；若有 SDK 事件则挂接 |
| Modify | `backend/app/runner.py` | 新增 `_on_disconnect(reason)`；在 `_initialize_runner` 注册 hook；扩展 `_run_loop` 检测 `_quote_subscribed` |
| Modify | `backend/app/services/runtime_state_service.py` | 新增 `record_runtime_state("BROKER_DISCONNECT", reason=...)` 入口（如果不存在则暴露为 AuditLogger 包装） |
| Create | `backend/tests/test_broker_disconnect.py` | 单测：hook 注册、回调触发、连续失败计数、审计写入 |
| Create | `backend/tests/test_runner_disconnect.py` | 集成测试：模拟 SDK 断线 → 5s 内重订 → 状态恢复 |

---

## 任务 1: 在 BrokerGateway 暴露 disconnect hook API

**Files:**
- Modify: `backend/app/core/broker.py:1-30`（顶部 import + 类签名）
- Modify: `backend/app/core/broker.py:200-260`（类内方法区）
- Test: `backend/tests/test_broker_disconnect.py`（新文件）

### Step 1.1: 写失败的测试（hook 注册与触发）

```python
# backend/tests/test_broker_disconnect.py
from app.core.broker import BrokerGateway


class _FakeQuoteContext:
    """Inline fake QuoteContext：模拟 longport SDK 行为，遵循项目无 pytest fixtures 约定。"""
    def __init__(self, supports_disconnect: bool = False) -> None:
        self._supports = supports_disconnect
        self._disconnect_handler = None
        self.on_disconnect = self._maybe_on_disconnect if supports_disconnect else None

    def _maybe_on_disconnect(self, handler):
        self._disconnect_handler = handler
        return self._disconnect_handler

    def simulate_disconnect(self, reason: str) -> None:
        if self._disconnect_handler:
            self._disconnect_handler(reason)
```

> **修订：** `_FakeQuoteContext` 改为 inline fake，定义在测试文件内部。**不创建** `backend/app/core/broker_sdk.py`（项目约定 no fixtures + no new infra file）。


def test_register_disconnect_hook_stores_callable():
    gw = BrokerGateway(_FakeQuoteContext())
    calls = []
    gw.register_disconnect_hook(lambda reason: calls.append(reason))
    assert len(gw._disconnect_hooks) == 1


def test_call_disconnect_hooks_invokes_all():
    gw = BrokerGateway(_FakeQuoteContext())
    a, b = [], []
    gw.register_disconnect_hook(lambda r: a.append(r))
    gw.register_disconnect_hook(lambda r: b.append(r))
    gw._call_disconnect_hooks("network_drop")
    assert a == ["network_drop"]
    assert b == ["network_drop"]


def test_call_disconnect_hooks_swallows_exceptions():
    gw = BrokerGateway(_FakeQuoteContext())
    calls = []
    gw.register_disconnect_hook(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    gw.register_disconnect_hook(lambda r: calls.append(r))
    gw._call_disconnect_hooks("x")  # 不应抛
    assert calls == ["x"]


def test_quote_context_with_disconnect_event_triggers_hook():
    ctx = _FakeQuoteContext(supports_disconnect=True)
    gw = BrokerGateway(ctx)
    calls = []
    gw.register_disconnect_hook(lambda r: calls.append(r))
    ctx.simulate_disconnect("auth_revoked")
    assert calls == ["auth_revoked"]


def test_quote_context_without_disconnect_event_does_not_break():
    ctx = _FakeQuoteContext(supports_disconnect=False)
    gw = BrokerGateway(ctx)  # 不应抛
    assert gw._disconnect_hooks == []
```

- [ ] **Step 1.1**: 把上述测试写入 `backend/tests/test_broker_disconnect.py`

### Step 1.2: 跑测试确认失败

```bash
cd backend && python3 -m pytest tests/test_broker_disconnect.py -v
```

预期：`ModuleNotFoundError: cannot import name 'BrokerGateway' from 'app.core.broker'` 或类似（因为 `register_disconnect_hook` 不存在）。

- [ ] **Step 1.2**: 跑测试，断言失败原因匹配

### Step 1.3: 写最小实现（BrokerGateway hook API）

```python
# backend/app/core/broker.py（添加到类内）
from __future__ import annotations
import logging
from typing import Callable, List

logger = logging.getLogger(__name__)

DisconnectHook = Callable[[str], None]


class BrokerGateway:
    def __init__(self, quote_ctx: object) -> None:
        self._quote_ctx = quote_ctx
        self._disconnect_hooks: List[DisconnectHook] = []
        self._register_native_disconnect_if_available()

    def register_disconnect_hook(self, hook: DisconnectHook) -> None:
        """注册 SDK 断线回调。重复注册同一 callable 会被去重。"""
        if hook not in self._disconnect_hooks:
            self._disconnect_hooks.append(hook)

    def _call_disconnect_hooks(self, reason: str) -> None:
        """触发所有已注册 hook；任一抛异常被吞掉，不影响其他 hook。"""
        for hook in list(self._disconnect_hooks):
            try:
                hook(reason)
            except Exception as exc:  # pragma: no cover - 防御
                logger.warning("disconnect_hook_failed", extra={"err": str(exc)})

    def _register_native_disconnect_if_available(self) -> None:
        """如 SDK 暴露原生 disconnect 事件，则挂接；否则仅保留看门兜底。"""
        on_disconnect = getattr(self._quote_ctx, "on_disconnect", None) or \
                        getattr(self._quote_ctx, "set_on_disconnect", None)
        if on_disconnect is None:
            logger.info("broker_disconnect_event_not_available_falling_back_to_watchdog")
            return
        try:
            on_disconnect(self._call_disconnect_hooks)
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("native_disconnect_register_failed", extra={"err": str(exc)})
```



- [ ] **Step 1.3**: 写入代码

### Step 1.4: 跑测试确认通过

```bash
cd backend && python3 -m pytest tests/test_broker_disconnect.py -v
```

预期：5 个测试全 PASS。

- [ ] **Step 1.4**: 跑测试，断言全绿

### Step 1.5: 跑全栈 + 类型检查

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：0 失败 / 0 errors / 0 warnings / 0 notes。

- [ ] **Step 1.5**: 跑全测 + pyright

### Step 1.6: Commit（默认不 commit，需用户显式指令）

```bash
git add backend/app/core/broker.py backend/tests/test_broker_disconnect.py
git commit -m "feat(broker): expose disconnect hook API with native event passthrough"
```

> ⚠️ **不主动 commit**。本步骤仅在用户要求时才执行；agent 在最后输出 "ready for commit, awaiting user approval"。

---

## 任务 2: AppRunner 处理 disconnect 事件 + 审计 + 重订

**Files:**
- Modify: `backend/app/runner.py:1-50`（import）
- Modify: `backend/app/runner.py:150-260`（`_run_loop` + `_initialize_runner`）
- Modify: `backend/app/services/runtime_state_service.py`（如需新增 `record_broker_event` 入口）
- Test: `backend/tests/test_runner_disconnect.py`（新文件）

### Step 2.1: 写失败的测试（AppRunner disconnect 处理）

```python
# backend/tests/test_runner_disconnect.py
from unittest.mock import MagicMock
from app.runner import AppRunner
from .test_broker_disconnect import _FakeQuoteContext  # 复用 inline fake


def test_on_disconnect_writes_audit_and_marks_unsubscribed():
    runner = AppRunner.__new__(AppRunner)  # 跳过 __init__
    runner.broker = MagicMock()
    runner._quote_subscribed = True
    runner._audit = MagicMock()
    runner._disconnect_retry_count = 0

    runner._on_disconnect("network_drop")

    runner._audit.record.assert_called_once()
    assert runner._quote_subscribed is False
    assert runner._disconnect_retry_count == 1


def test_on_disconnect_increments_retry_count_each_call():
    runner = AppRunner.__new__(AppRunner)
    runner.broker = MagicMock()
    runner._quote_subscribed = True
    runner._audit = MagicMock()
    runner._disconnect_retry_count = 0

    runner._on_disconnect("a")
    runner._on_disconnect("b")
    runner._on_disconnect("c")
    runner._on_disconnect("d")  # 第 4 次

    assert runner._disconnect_retry_count == 4


def test_on_disconnect_writes_retry_exhausted_audit_at_threshold():
    runner = AppRunner.__new__(AppRunner)
    runner.broker = MagicMock()
    runner._quote_subscribed = True
    runner._audit = MagicMock()
    runner._disconnect_retry_count = 2  # 下一次进入即 3

    runner._on_disconnect("c")

    # 第 3 次应额外写 BROKER_RETRY_EXHAUSTED
    actions = [c.kwargs.get("action") for c in runner._audit.record.call_args_list]
    assert "BROKER_RETRY_EXHAUSTED" in actions


def test_run_loop_resubscribes_when_unsubscribed():
    runner = AppRunner.__new__(AppRunner)
    runner.broker = MagicMock()
    runner._quote_subscribed = False
    runner._disconnect_retry_count = 1
    runner._last_quote_at = None

    runner._on_resubscribe_if_needed()

    runner.broker.unsubscribe_all.assert_called_once()
    runner.broker.subscribe.assert_called_once()
    assert runner._quote_subscribed is True
    assert runner._disconnect_retry_count == 0
```

- [ ] **Step 2.1**: 写入测试

### Step 2.2: 跑测试确认失败

```bash
cd backend && python3 -m pytest tests/test_runner_disconnect.py -v
```

预期：`AttributeError: 'AppRunner' object has no attribute '_on_disconnect'`。

- [ ] **Step 2.2**: 跑测试，确认失败

### Step 2.3: 写实现（AppRunner._on_disconnect + _on_resubscribe_if_needed）

```python
# backend/app/runner.py（添加到类内）
DISCONNECT_RETRY_EXHAUSTED_THRESHOLD = 3


class AppRunner:
    def _initialize_runner(self) -> None:
        # ... 现有代码 ...
        self._quote_subscribed: bool = False
        self._disconnect_retry_count: int = 0
        # 注册 broker disconnect hook
        if self.broker is not None:
            self.broker.register_disconnect_hook(self._on_disconnect)

    def _on_disconnect(self, reason: str) -> None:
        """BrokerGateway hook：写审计 + 标记未订阅 + 累计重订次数。"""
        logger.warning("broker_disconnect", extra={"reason": str(reason)})
        try:
            self._audit.record(
                action="BROKER_DISCONNECT",
                severity="WARNING",
                reason=str(reason),
            )
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("audit_record_failed", extra={"err": str(exc)})
        self._quote_subscribed = False
        self._disconnect_retry_count += 1
        if self._disconnect_retry_count >= DISCONNECT_RETRY_EXHAUSTED_THRESHOLD:
            try:
                self._audit.record(
                    action="BROKER_RETRY_EXHAUSTED",
                    severity="CRITICAL",
                    reason=f"disconnect_retry_count={self._disconnect_retry_count}",
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("audit_record_failed", extra={"err": str(exc)})
            # 不自动 pause，保留手动干预

    def _on_resubscribe_if_needed(self) -> None:
        """重订 quote 订阅。失败抛异常由 _run_loop 兜底。"""
        if self._quote_subscribed:
            return
        if self.broker is None:
            return
        symbols = self._get_subscribed_symbols()  # 现有方法
        self.broker.unsubscribe_all()
        self.broker.subscribe(symbols)
        self._last_quote_at = now()
        self._quote_subscribed = True
        self._disconnect_retry_count = 0
```

同时在 `_run_loop` 头部加一行：

```python
# backend/app/runner.py:_run_loop（顶部现有代码后插入）
def _run_loop(self) -> None:
    while not self._stop_event.is_set():
        try:
            self._on_resubscribe_if_needed()  # 新增
            # ... 现有 _run_loop 主流程 ...
        except Exception as exc:
            logger.exception("run_loop_error", extra={"err": str(exc)})
            self._stop_event.wait(5)
```

- [ ] **Step 2.3**: 写入代码

### Step 2.4: 跑测试确认通过

```bash
cd backend && python3 -m pytest tests/test_runner_disconnect.py -v
```

预期：4 个测试全 PASS。

- [ ] **Step 2.4**: 跑测试，断言全绿

### Step 2.5: 跑全栈质量门禁

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：0 失败 / 0 errors / 0 warnings。

- [ ] **Step 2.5**: 跑全测 + pyright

### Step 2.6: Commit（用户显式指令时执行）

```bash
git add backend/app/runner.py backend/tests/test_runner_disconnect.py
git commit -m "feat(runner): handle broker disconnect event with audit + resubscribe"
```

> ⚠️ **不主动 commit**。agent 在最后输出 "ready for commit, awaiting user approval"。

---

## 任务 3: E2E 集成测试（hook → 审计 → 重订完整链路）

**Files:**
- Modify: `backend/tests/test_e2e_restart.py`（追加新场景到现有 e2e 测试集）
- Test: 现有 `TestClient` + FakeBroker 模式

### Step 3.1: 写 e2e 场景

在 `backend/tests/test_e2e_restart.py` 的 `TestE2ERestart` 类中追加：

```python
def test_broker_disconnect_triggers_resubscribe_within_5s(self):
    """模拟 broker 断线，断言 ≤5s 内重订、审计写入、未触发 pause。"""
    from app.main import app
    from app.database import SessionLocal
    from app.models import AuditLog

    with TestClient(app) as client:
        # 启动 runner
        r = client.post("/api/control/start")
        assert r.status_code == 200

        # 注入 disconnect
        runner = get_runner()
        runner.broker.simulate_disconnect("test_network_drop")

        # 等待 5s（最长一个 tick）
        time.sleep(5.5)

        # 断言：审计写入
        with SessionLocal() as db:
            actions = [row.action for row in db.query(AuditLog).all()]
        assert "BROKER_DISCONNECT" in actions

        # 断言：未触发 pause（仍可下单）
        status = client.get("/api/status").json()
        assert status["paused"] is False
        # runner 仍标记已订阅（已重订）
        assert runner._quote_subscribed is True
```

- [ ] **Step 3.1**: 追加到 e2e 测试文件

### Step 3.2: 跑 e2e 确认通过

```bash
cd backend && python3 -m pytest tests/test_e2e_restart.py -v
```

预期：原有 5 场景 + 新增 1 场景全 PASS。

- [ ] **Step 3.2**: 跑测试，断言全绿

### Step 3.3: 跑全栈 + 前端 + Cypress

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全部 exit 0。

- [ ] **Step 3.3**: 跑全栈 + Cypress

### Step 3.4: 写证据文件

```bash
mkdir -p .sisyphus/evidence
cat > .sisyphus/evidence/task-A-sdk-disconnect.txt << 'EOF'
Task A: P5' SDK disconnect 回调接入
完成时间: <YYYY-MM-DD>
测试: <new tests passed> / <total>
新文件: backend/tests/test_broker_disconnect.py, backend/tests/test_runner_disconnect.py
修改: backend/app/core/broker.py, backend/app/runner.py
新增 e2e: test_broker_disconnect_triggers_resubscribe_within_5s
EOF
```

- [ ] **Step 3.4**: 写证据

### Step 3.5: Commit

```bash
git add backend/tests/test_e2e_restart.py .sisyphus/evidence/task-A-sdk-disconnect.txt
git commit -m "test(e2e): cover broker disconnect → resubscribe flow"
```

> ⚠️ **不主动 commit**。agent 输出 "Task A complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 5 + 4 + 1 = 10 个新测试全绿
- [ ] 全栈 `pytest` 通过
- [ ] `basedpyright` 0 errors / 0 warnings / 0 notes
- [ ] `npm run type-check` + `build` + `cypress:run` 通过
- [ ] 证据文件 `.sisyphus/evidence/task-A-sdk-disconnect.txt` 已写
- [ ] **不主动 commit**（等用户指令）
- [ ] Roadmap.md 第 154-160 行更新 P5' 为完成（如有需要；本任务不强制）

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| SDK 不暴露 disconnect 事件 | 看门狗兜底（已就位）；测试覆盖"无 disconnect 支持"路径 |
| 重订风暴 | 连续失败 ≥3 次只审计不自动 pause；用户手动 disable / 重启 |
| Hook 异常影响主流程 | `_call_disconnect_hooks` 吞掉 hook 异常 |
| 审计写失败影响断线处理 | `_on_disconnect` 内 try/except 包 audit 写入 |

## 范围外（YAGNI）

- ❌ 自动 pause（连续失败时）
- ❌ 改 SDK 内部行为
- ❌ 修改 longport SDK 调用方式
- ❌ 修改现有 90s 静默看门狗逻辑

---

**Plan A 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.1 + §5.1。**
