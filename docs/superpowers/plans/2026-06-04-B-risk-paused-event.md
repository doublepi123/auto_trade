# B. P23a' Concern B：RISK_PAUSED 事件补写 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `_pause_if_unresolved_live_order_exists` 现有 `risk.pause + logger.warning` 之外，写一条 `RISK_PAUSED` 事件到决策时间线，关闭 P23a' Concern B。

**Architecture:** 修改 `AppRunner._pause_if_unresolved_live_order_exists`，在 pause 后追加 `TradeEventService.record_event("RISK_PAUSED", ...)` 调用。异常吞掉，不阻塞 pause 主流程（沿用 AuditLogger 模式）。

**Tech Stack:** SQLAlchemy 2.0 / pytest 9 / basedpyright / project inline-fake 约定

**前置阅读：**
- 母 spec §4.2 + §5.2
- `backend/app/runner.py:_pause_if_unresolved_live_order_exists`（现有实现）
- `backend/app/services/trade_event_service.py:record_event`（API 签名）

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Modify | `backend/app/runner.py:_pause_if_unresolved_live_order_exists` | 在 pause 成功后追加 `record_event` 调用 |
| Modify | `backend/app/runner.py`（import 段） | 引入 `TradeEventService` |
| Create | `backend/tests/test_runner_risk_paused_event.py` | 单测 3 个：事件存在 / payload 完整 / 异常吞掉 |

---

## 任务 1: 修改 AppRunner 写 RISK_PAUSED 事件

**Files:**
- Modify: `backend/app/runner.py:1-30`（import）
- Modify: `backend/app/runner.py:_pause_if_unresolved_live_order_exists`（方法体）
- Test: `backend/tests/test_runner_risk_paused_event.py`（新文件）

### Step 1.1: 写失败的测试

```python
# backend/tests/test_runner_risk_paused_event.py
from unittest.mock import MagicMock, patch
from app.runner import AppRunner


def _make_runner(unsync_orders_count: int = 1):
    runner = AppRunner.__new__(AppRunner)
    runner.risk = MagicMock()
    runner._trade_event_service = MagicMock()
    runner.broker = MagicMock()
    runner.broker.today_orders = [MagicMock(id=f"order-{i}") for i in range(unsync_orders_count)]
    return runner


def test_risk_paused_event_recorded_after_pause():
    runner = _make_runner(unsync_orders_count=1)
    with patch("app.runner.trade_day_for", return_value="2026-06-04"):
        result = runner._pause_if_unresolved_live_order_exists(market="US")

    assert result is True
    runner._trade_event_service.record_event.assert_called_once()
    kwargs = runner._trade_event_service.record_event.call_args.kwargs
    assert kwargs["event_type"] == "RISK_PAUSED"
    assert kwargs["source"] == "runner"
    assert kwargs["symbol"] is None
    assert kwargs["detail"]["reason"] == "unresolved_live_order"
    assert kwargs["detail"]["live_order_id"] == "order-0"
    assert kwargs["detail"]["trade_day"] == "2026-06-04"


def test_risk_paused_event_payload_complete():
    runner = _make_runner(unsync_orders_count=2)
    with patch("app.runner.trade_day_for", return_value="2026-06-05"):
        runner._pause_if_unresolved_live_order_exists(market="HK")

    detail = runner._trade_event_service.record_event.call_args.kwargs["detail"]
    assert set(detail.keys()) == {"reason", "live_order_id", "trade_day"}


def test_record_event_exception_does_not_block_pause():
    runner = _make_runner(unsync_orders_count=1)
    runner._trade_event_service.record_event.side_effect = RuntimeError("db error")
    with patch("app.runner.trade_day_for", return_value="2026-06-04"):
        result = runner._pause_if_unresolved_live_order_exists(market="US")

    # pause 主流程仍走完
    assert result is True
    runner.risk.pause.assert_called_once()


def test_no_event_when_no_unsynced_orders():
    runner = _make_runner(unsync_orders_count=0)
    result = runner._pause_if_unresolved_live_order_exists(market="US")
    assert result is False
    runner._trade_event_service.record_event.assert_not_called()
```

- [ ] **Step 1.1**: 写入测试

### Step 1.2: 跑测试确认失败

```bash
cd backend && python3 -m pytest tests/test_runner_risk_paused_event.py -v
```

预期：4 个测试 FAIL（`_pause_if_unresolved_live_order_exists` 尚未调用 `record_event`）。

- [ ] **Step 1.2**: 跑测试，断言失败

### Step 1.3: 修改 _pause_if_unresolved_live_order_exists

```python
# backend/app/runner.py（import 段添加）
from app.core.market_calendar import trade_day_for
from app.services.trade_event_service import TradeEventService  # 如未导入


# backend/app/runner.py:_pause_if_unresolved_live_order_exists（修改）
def _pause_if_unresolved_live_order_exists(self, market: str) -> bool:
    """若存在未同步券商侧的 live order，写 RISK_PAUSED 事件并 pause。"""
    if not self.broker or not hasattr(self.broker, "today_orders"):
        return False
    unsynced = [o for o in self.broker.today_orders if o.status not in ("FILLED", "CANCELLED", "REJECTED")]
    if not unsynced:
        return False
    try:
        self.risk.pause(reason="unresolved_live_order")
        logger.warning("risk_paused_due_to_unsynced_live_order",
                       extra={"order_count": len(unsynced), "market": market})
    except Exception as exc:
        logger.exception("risk_pause_failed", extra={"err": str(exc)})
        return False

    # 写 RISK_PAUSED 事件（沿用 AuditLogger 异常吞掉模式）
    try:
        TradeEventService.record_event(
            event_type="RISK_PAUSED",
            source="runner",
            symbol=None,
            detail={
                "reason": "unresolved_live_order",
                "live_order_id": unsynced[0].id,
                "trade_day": str(trade_day_for(market, now())),
            },
        )
    except Exception as exc:
        logger.warning("risk_paused_event_record_failed", extra={"err": str(exc)})

    return True
```

> **注意：** `TradeEventService` 可能是模块级函数式（看现有 `record_event` API）。如需实例化，调整为 `self._trade_event_service.record_event(...)`。

- [ ] **Step 1.3**: 修改代码

### Step 1.4: 跑测试确认通过

```bash
cd backend && python3 -m pytest tests/test_runner_risk_paused_event.py -v
```

预期：4 个测试全 PASS。

- [ ] **Step 1.4**: 跑测试，断言全绿

### Step 1.5: 跑全栈 + 类型检查

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：0 失败 / 0 errors。

- [ ] **Step 1.5**: 跑全测 + pyright

### Step 1.6: 写证据 + Commit（用户显式指令时执行）

```bash
mkdir -p .sisyphus/evidence
cat > .sisyphus/evidence/task-B-risk-paused-event.txt << 'EOF'
Task B: P23a' Concern B - RISK_PAUSED 事件补写
完成时间: <YYYY-MM-DD>
测试: 4 个新测试全绿
新文件: backend/tests/test_runner_risk_paused_event.py
修改: backend/app/runner.py
关闭: P23a' Concern B
EOF

git add backend/app/runner.py backend/tests/test_runner_risk_paused_event.py .sisyphus/evidence/task-B-risk-paused-event.txt
git commit -m "feat(runner): record RISK_PAUSED event for unresolved live orders (P23a' Concern B)"
```

> ⚠️ **不主动 commit**。agent 输出 "Task B complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 4 个新测试全绿
- [ ] 全栈 `pytest` 通过
- [ ] `basedpyright` 0 errors
- [ ] 证据文件已写
- [ ] **不主动 commit**
- [ ] Roadmap.md 第 546 行 P23a' Concern B 标记关闭（如本任务与 Roadmap 同步一并做）

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| TradeEventService 写失败 | try/except 吞掉，不阻塞 pause |
| market 缺失 | 已在 `strategy_service` 持有；若 None 写 `""` 或跳过事件 |
| 多次 pause 写多个事件 | `_pause_if_unresolved_live_order_exists` 已通过 `risk.paused` 短路（不重复写） |

## 范围外（YAGNI）

- ❌ 改 `RiskController.pause()` 签名
- ❌ 写 `audit_logs`（事件由 TradeEventService 负责）
- ❌ 修改现有 P5+ 多渠道通知逻辑
- ❌ 修改其他 P5+ 事件源

---

**Plan B 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.2 + §5.2。**
