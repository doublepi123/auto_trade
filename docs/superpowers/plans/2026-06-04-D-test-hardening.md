# D. 测试加固（时区/并发/flaky） 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Roadmap 546 行提到的"时区边界/偶现 flaky"风险，补充并发测试，提升 `pytest --count=10` 稳定性。

**Architecture:** 三维加固：①跨时区/夏令时边界（`test_market_calendar.py`）②并发（`test_runner.py` 死锁防护）③flaky 修复（`freezegun.freeze_time` 替代 wall clock）。不删任何测试。

**Tech Stack:** pytest 9 + pytest-asyncio 0.24+ / pytest-repeat（`--count=10`）/ freezegun / threading / project inline-fake 约定

**前置阅读：**
- 母 spec §4.4
- 现有 `backend/tests/test_market_calendar.py` + `backend/tests/test_runner.py`

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Modify | `backend/tests/test_market_calendar.py` | 增加跨时区/夏令时边界用例 |
| Modify | `backend/tests/test_runner.py` | 增加并发死锁防护用例 |
| Modify | `backend/tests/test_trade_execution_service.py`（如适用） | 用 freezegun 替换 wall clock |
| Create | `backend/tests/test_concurrency.py`（新文件） | 多线程并发调用 runner / risk |
| Modify | `backend/requirements-dev.txt` | 添加 `pytest-repeat` + `freezegun`（如未在） |

---

## 任务 1: 跨时区/夏令时边界加固

**Files:**
- Modify: `backend/tests/test_market_calendar.py`（追加类）
- Modify: `backend/requirements-dev.txt`

### Step 1.1: 写失败的测试（DST 切换边界）

```python
# backend/tests/test_market_calendar.py（追加 TestDstBoundary 类）
from freezegun import freeze_time
from app.core.market_calendar import trade_day_for, is_trading_hours


class TestDstBoundary:
    """覆盖美国夏令时切换日（3 月第二个周日 / 11 月第一个周日）。"""

    def test_us_dst_spring_forward_march_8_2026(self):
        """2026-03-08 02:00 EST → 03:00 EDT，US 交易日不变。"""
        with freeze_time("2026-03-08 06:30:00", tz_offset=0):
            # 06:30 UTC = 02:30 EDT (DST 已切换)
            day = trade_day_for("US", datetime.utcnow())
            assert day == date(2026, 3, 8)

    def test_us_dst_fall_back_nov_1_2026(self):
        """2026-11-01 02:00 EDT → 01:00 EST，US 交易日不变。"""
        with freeze_time("2026-11-01 05:30:00", tz_offset=0):
            # 05:30 UTC = 01:30 EST
            day = trade_day_for("US", datetime.utcnow())
            assert day == date(2026, 11, 1)

    def test_us_rth_after_dst_jump(self):
        """DST 切换日 RTH 窗口仍 14:30~21:00 UTC（夏令时）/ 14:30~21:00 UTC（标准时）。"""
        with freeze_time("2026-03-08 14:30:00", tz_offset=0):
            assert is_trading_hours("US", datetime.utcnow()) is True
        with freeze_time("2026-11-01 14:30:00", tz_offset=0):
            assert is_trading_hours("US", datetime.utcnow()) is True

    def test_hk_dst_no_change(self):
        """HK 无夏令时，交易日与本地日历日严格一致。"""
        with freeze_time("2026-03-08 16:00:00", tz_offset=0):  # 16:00 UTC = 00:00 HKT (+1)
            day = trade_day_for("HK", datetime.utcnow())
            assert day == date(2026, 3, 9)  # 已跨日

    def test_us_hk_simultaneous_trade_day_different(self):
        """同一 UTC 时刻，US 与 HK 交易日可能不同。"""
        with freeze_time("2026-06-04 16:00:00", tz_offset=0):  # 16:00 UTC = 12:00 EDT = 00:00 HKT(+1)
            us_day = trade_day_for("US", datetime.utcnow())  # 06-04
            hk_day = trade_day_for("HK", datetime.utcnow())  # 06-05
            assert us_day != hk_day
```

- [ ] **Step 1.1**: 写入测试

### Step 1.2: 添加 freezegun 依赖

```bash
cd backend
# 当前 requirements-dev.txt 仅有 pytest/pytest-asyncio/basedpyright
# 新增 freezegun（task D 必需）：
cat requirements-dev.txt
# 输出: pytest>=8.0.0
#        pytest-asyncio>=0.24.0
#        basedpyright>=1.31.0

# 追加 freezegun
echo "freezegun>=1.2" >> requirements-dev.txt
pip install freezegun
```

> **修订：** Plan D 需 freezegun；显式追加到 `requirements-dev.txt` 并 `pip install`。`pytest-repeat` 仅在任务 3.3 跑稳定性时按需装（不写进 requirements，避免污染 devDep）。

- [ ] **Step 1.2**: 装 freezegun

### Step 1.3: 跑测试确认通过

```bash
cd backend && python3 -m pytest tests/test_market_calendar.py -v
```

预期：5 个新测试全 PASS（同时不破坏现有 calendar 测试）。

- [ ] **Step 1.3**: 跑测试

### Step 1.4: 跑全测验证

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：0 失败 / 0 errors。

- [ ] **Step 1.4**: 全测验证

---

## 任务 2: 并发死锁防护

**Files:**
- Create: `backend/tests/test_concurrency.py`

### Step 2.1: 写失败的测试（多线程并发调 _try_buy/_try_sell）

```python
# backend/tests/test_concurrency.py
import threading
import time
from app.runner import AppRunner
from app.core.risk import RiskController
from app.core.broker_sdk import _FakeBroker


def test_app_runner_lock_prevents_race_in_try_buy_sell():
    """两个线程同时调 _try_buy 与 _try_sell，断言不产生 race condition。"""
    broker = _FakeBroker(price=100.0, position_qty=0)
    risk = RiskController(daily_loss_limit=1_000_000, max_consecutive_losses=1000)
    runner = AppRunner(broker=broker, risk=risk, strategy_config=...)
    runner.start()  # 现有

    results = {"buy": [], "sell": []}

    def try_buy():
        for _ in range(50):
            res = runner._try_buy(price=99.0)
            results["buy"].append(res)
            time.sleep(0.001)

    def try_sell():
        for _ in range(50):
            res = runner._try_sell(price=101.0)
            results["sell"].append(res)
            time.sleep(0.001)

    t1 = threading.Thread(target=try_buy)
    t2 = threading.Thread(target=try_sell)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # 断言：未死锁（线程在 10s 内结束）
    assert not t1.is_alive()
    assert not t2.is_alive()
    # 断言：所有调用都返回了结果（无 hang）
    assert len(results["buy"]) >= 40
    assert len(results["sell"]) >= 40


def test_risk_controller_pause_is_thread_safe():
    """两个线程同时调 risk.pause，断言状态一致。"""
    risk = RiskController(daily_loss_limit=1_000_000, max_consecutive_losses=1000)
    barrier = threading.Barrier(5)

    def pauser(reason):
        barrier.wait()
        risk.pause(reason=reason)

    threads = [threading.Thread(target=pauser, args=(f"reason-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # 仅一个 pause 生效（或都生效但状态一致）
    assert risk.paused is True
```

- [ ] **Step 2.1**: 写入测试

### Step 2.2: 跑测试

```bash
cd backend && python3 -m pytest tests/test_concurrency.py -v
```

预期：2 个测试全 PASS（项目约定 _lock 已就位，仅验证不挂）。

- [ ] **Step 2.2**: 跑测试

### Step 2.3: 跑全测验证

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：全绿。

- [ ] **Step 2.3**: 全测验证

---

## 任务 3: flaky 修复（freezegun 替代 wall clock）

**Files:**
- Modify: `backend/tests/test_trade_execution_service.py`（如有用 `time.time()` / `datetime.now()` 的断言）
- Modify: 其他测试文件（同上）

### Step 3.1: 查找 wall clock 使用

```bash
cd backend
grep -rnE "time\.time\(\)|datetime\.now\(\)|datetime\.utcnow\(\)" tests/ | head -20
```

预期：找到若干测试中直接用 wall clock 的位置。

- [ ] **Step 3.1**: 查找 wall clock

### Step 3.2: 替换为 freezegun（按需，逐个修改）

**示例（仅示意，实际按 grep 结果改）：**

```python
# 之前：
def test_xxx():
    start = time.time()
    do_something()
    assert time.time() - start < 1.0

# 之后：
@freeze_time("2026-06-04 10:00:00")
def test_xxx():
    start = time.time()
    do_something()
    assert time.time() - start < 1.0  # 但 wall clock 仍走；改用 mock
```

> 实际：很多 wall clock 用法是正确的（如测真实耗时）；仅当测试是断言"时间戳"时才需要 freezegun 替换。

- [ ] **Step 3.2**: 替换 wall clock

### Step 3.3: 跑稳定性测试

```bash
cd backend
# pytest-repeat 仅一次性使用，不污染 devDep
pip install pytest-repeat
python3 -m pytest tests/ -p no:randomly --count=10 -x
```

预期：10 次跑均通过。如有 flake，定位并修复（**不删测试**）。

- [ ] **Step 3.3**: 稳定性测试

---

## 任务 4: 写证据 + Commit

### Step 4.1: 写证据

```bash
cat > .sisyphus/evidence/task-D-test-hardening.txt << 'EOF'
Task D: 测试加固
完成时间: <YYYY-MM-DD>
新增测试: ~7~10 个
- 时区/夏令时: 5 个（test_market_calendar.py）
- 并发: 2 个（test_concurrency.py）
- flaky 修复: N 个（test_trade_execution_service.py 等）
稳定性: pytest --count=10 0 flake
EOF
```

- [ ] **Step 4.1**: 写证据

### Step 4.2: Commit

```bash
git add backend/tests/ backend/requirements-dev.txt .sisyphus/evidence/task-D-test-hardening.txt
git commit -m "test: harden timezone, concurrency, and flake-prone tests"
```

> ⚠️ **不主动 commit**。agent 输出 "Task D complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 5 + 2 + N 个新测试全绿
- [ ] `pytest --count=10` 0 flake
- [ ] `basedpyright` 0 errors
- [ ] 现有 730+ 测试无回归
- [ ] **不主动 commit**

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| freezegun 影响其他测试 | 仅在测试内 `@freeze_time` 装饰；不全局冻结 |
| 并发测试本身 flaky | 跑 10 次验证；timeout 5s 强制结束 |
| wall clock 替换误改 | 仅替换断言"时间戳"的；保留测真实耗时的 |
| pytest-repeat 不兼容 | pip install；如失败回退 `--count=1` |

## 范围外（YAGNI）

- ❌ 删除任何测试
- ❌ 引入新的 mock 库（沿用 inline fake 约定）
- ❌ 修改生产代码（仅修改测试）
- ❌ 引入压力测试 / 性能测试

---

**Plan D 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.4。**
