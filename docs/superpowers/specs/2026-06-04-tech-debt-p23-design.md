# 下一迭代：技术债清扫 + P23 前端实时通知中心 设计

> **日期：** 2026-06-04
> **代号：** P24（综合迭代：技术债清扫 + P23 Toast 浮层）
> **基线：** commit `c2fad57`（P23a' 2026-06-03 已交付），`pytest 730 passed` / `basedpyright 0 errors / 0 warnings / 0 notes` / `vue-tsc` clean / Cypress 全绿
> **目标分支：** `main`
> **前置阅读：**
> - `docs/Roadmap.md`（P1~P23a' 全部交付，本迭代为下一轮）
> - `docs/superpowers/specs/2026-05-26-audit-notification-trading-safety-design.md`（多渠道通知已就位，P23 Toast 浮层复用其 severity 分级）
> - `docs/superpowers/specs/2026-06-02-llm-intelligent-interval-design.md`（LLM 提示架构）
> - `docs/superpowers/plans/2026-05-31-next-iteration-roadmap.md`（P13~P22 排期历史，本迭代不在该排期内）

---

## 1. 背景与动机

P1~P23a' 完整交付后系统已具备 730 项 pytest + 80+ Cypress + CI 质量门禁，Roadmap 中明确跟踪的开放项收敛为两点：
- **P5' SDK disconnect 回调** —— 当前仅做了 `lifespan` 非阻塞与 90s 静默看门兜底，未接 SDK disconnect 事件（Roadmap 154-160 行）。
- **P23a' Concern B** —— `_pause_if_unresolved_live_order_exists` 仅 `logger.warning + risk.pause`，不写 `RISK_PAUSED` 事件到决策时间线（Roadmap 546 行）。

同时存在 4 个落后于 main 的本地分支（`feature/addon-buy-margin-sizing` ahead 7 / `feature/dashboard-config-performance` ahead 1 / `p5-plus-audit-notifications` ahead 1 / `refactor/maintainability-frontend` ahead 1）已对后续迭代构成 rebase 冲突风险。

用户体验层，Dashboard 当前要求"必须刷新"才能看到最近风控/跳过/审计事件，是 Roadmap 显式建议的 P23 待启动项。

本迭代在保持现有质量基线的前提下：
1. 关闭 P5' SDK disconnect + P23a' Concern B 两个 Roadmap 跟踪项
2. 清扫技术债（死代码、测试加固、前端 ai-slop）
3. 合并 4 个落后分支，消除后续迭代冲突
4. 交付 P23 前端实时通知中心（Toast 浮层）
5. 严格遵守 YAGNI，不引入新的长期债务

---

## 2. 范围与切除

### 2.1 范围（7 个任务）

| ID | 任务 | 来源 | 估时 | 涉及模块 |
|----|------|------|------|----------|
| **A** | P5' SDK disconnect 回调接入 | Roadmap 154-160 | 1~1.5 天 | `backend/app/core/broker.py` / `backend/app/runner.py` |
| **B** | P23a' Concern B：`RISK_PAUSED` 事件补写 | Roadmap 546 | 0.5 天 | `backend/app/runner.py` / `backend/app/services/trade_event_service.py` |
| **C** | 死代码最终清理 | Roadmap 268-270（P8' 未完） | 0.5 天 | 全栈 AST 扫描 + 双向 grep |
| **D** | 测试加固（时区/并发/flaky） | Roadmap 546（暗示） | 1 天 | `backend/tests/*` |
| **E** | 合并 4 个活跃分支到 main | git 落后状态 | 1.5~2 天 | git rebase + squash + 合入 |
| **F** | 前端 ai-slop 清理 | 经验性 | 1 天 | `frontend/src/**` |
| **P23** | 前端实时通知中心 · Toast 浮层 | Roadmap 558-560 | 1.5~2 天 | `frontend/src/composables/useNotificationStream.ts`（新）+ `Dashboard.vue` 改造 |

### 2.2 3 波分组（4-3-2 命名约定）

| 波次 | 主题 | 任务 | 估时 | 并行策略 |
|------|------|------|------|----------|
| **Wave 1** | 后端韧性 | A + B | 1.5~2 天 | 2 subagent 并行（独立模块） |
| **Wave 2** | 质量清扫 | D + C + F | 2~2.5 天 | 3 subagent 并行（测试 / 后端扫描 / 前端扫描） |
| **Wave 3** | 体验 + 合流 | P23 + E | 3~3.5 天 | 2 subagent 并行（前端体验 / git 合流） |

**合计：** 7 任务 / 估时 6.5~8 天 / 实际 ~1.5~2 周（含 buffer 与 review）

**波间依赖：**
- Wave 2 依赖 Wave 1 合入后的代码基（`pytest` + `basedpyright` 全绿）
- Wave 3 依赖 Wave 1+2 合入；P23 需要 Wave 2 中 F 任务扫出的 testid 列表作为通知测试夹具

### 2.3 显式 YAGNI 切除

- ❌ **节假日历** —— Roadmap 显式 YAGNI 保留
- ❌ **审计 CSV/JSON 导出** —— Roadmap 显式 YAGNI 保留
- ❌ **Webhook 模板编辑器** —— Roadmap 显式 YAGNI 保留
- ❌ **通知重发队列** —— Roadmap 显式 YAGNI 保留
- ❌ **API 鉴权收紧（P2）** —— owner 2026-05-25 决策不实施
- ❌ **多标的自动交易（P24-原）** —— 架构风险高，单独立项评估
- ❌ **PWA 离线支持** —— 移动端基础版已就位，锦上添花
- ❌ **高频交易 / 复杂择时指标 / 量化研究平台** —— 设计限制
- ❌ **代客理财 / 公开策略分发** —— 法律合规限制，永久不在计划
- ❌ **修改 longport SDK 行为 / trading session guard 语义 / risk/cooldown/fee guard 现有行为** —— 越界即停
- ❌ **主动 commit** —— 默认不 commit，遵循项目约定，等用户显式指令
- ❌ **改 main 分支策略 / 改 CI 发布流程** —— 越界即停
- ❌ **重写组件、修改 props/refs/事件流（任务 F/P23 限制）** —— 仅去异味 / 新增 composable

---

## 3. 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                       Wave 1: 后端韧性                             │
│  ┌────────────────────────┐  ┌──────────────────────────────┐    │
│  │ A. SDK disconnect 回调  │  │ B. RISK_PAUSED 事件补写       │    │
│  │ BrokerGateway         │  │ runner._pause_if_un...       │    │
│  │ _on_disconnect hook   │  │ + TradeEventService           │    │
│  │ ↳ AppRunner._resub  │  │   .record_event("RISK_PAUSED") │    │
│  └────────────────────────┘  └──────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
                              ↓ 合入 main
┌──────────────────────────────────────────────────────────────────┐
│                       Wave 2: 质量清扫                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐        │
│  │ D. 测试加固  │  │ C. 死代码    │  │ F. 前端 ai-slop   │        │
│  │ 时区/并发   │  │ AST 扫描    │  │ 重复/魔术串     │        │
│  │ mock broker │  │ 未用 import │  │ 缺失 testid     │        │
│  │ 边界用例    │  │ 未用 schema │  │ Vue/Pinia 异味  │        │
│  └──────────────┘  └──────────────┘  └──────────────────┘        │
└──────────────────────────────────────────────────────────────────┘
                              ↓ 合入 main
┌──────────────────────────────────────────────────────────────────┐
│                     Wave 3: 体验 + 合流                            │
│  ┌──────────────────────────┐  ┌──────────────────────────┐      │
│  │ P23. Toast 实时通知      │  │ E. 4 分支 rebase + 合并  │      │
│  │ useNotificationStream  │  │ addon-buy-margin (7)    │      │
│  │ composable             │  │ dashboard-config-perf(1)│      │
│  │ + Element Plus         │  │ p5-plus-audit (1)       │      │
│  │   ElNotification       │  │ maintainability (1)     │      │
│  │ 按 severity 分级        │  │ squash + 冲突解决       │      │
│  └──────────────────────────┘  └──────────────────────────┘      │
└──────────────────────────────────────────────────────────────────┘
```

**关键架构约束：**
- Wave 内部任务通过并行 subagent 推进；Wave 间因代码基漂移必须串行合入
- 每个 subagent 仅修改本任务范围内的文件，跨任务冲突点立即升级
- 7 个 plan 文件与本 spec 同目录（`docs/superpowers/plans/2026-06-04-{id}-*.md`），是 subagent 的执行指令

---

## 4. 七任务关键设计

### 4.1 A. P5' SDK disconnect 回调接入

**目标：** 在现有 90s 静默看门狗基础上，优先接 longport SDK 的 disconnect 事件回调；如 SDK 不暴露则保留看门兜底。

**关键决策：**
- 接 `longport.QuoteContext._on_disconnect(reason)` 回调（如 SDK 暴露），回调内调用 `AppRunner._on_disconnect(reason)`
- `AppRunner._on_disconnect(reason)` 行为：
  1. `logger.warning("broker_disconnect", extra={"reason": str(reason)})`
  2. 写 `audit_logs(action="BROKER_DISCONNECT", severity="WARNING", reason=str(reason))`
  3. `_quote_subscribed = False`
- 下一 tick（≤5s）内 `_run_loop` 检测 `_quote_subscribed == False` → `unsubscribe_all() + subscribe(symbols) + _last_quote_at = now()`
- **不自动 pause**：连续重订失败 ≥3 次写 `BROKER_RETRY_EXHAUSTED` 审计，保留手动干预

**保留不动的部分：**
- 90s 静默看门狗（兜底）
- `_call_with_retry` 退避逻辑（P5+ 已就位）
- `_auto_resumable_pause_reason` 路径

**风险：** SDK 不暴露 disconnect 事件时退化运行；测试用 `FakeBroker` 注入"60s 无 quote + 回调触发"双场景。

### 4.2 B. P23a' Concern B：`RISK_PAUSED` 事件补写

**目标：** 在 `_pause_if_unresolved_live_order_exists` 现有 `risk.pause + logger.warning` 之外，写一条 `RISK_PAUSED` 事件到决策时间线。

**关键决策：**
- 沿用 `TradeEventService.record_event` 模式（与 `TRACKED_ENTRY_DRIFT` 同源）
- 事件 payload：
  ```python
  {
      "event_type": "RISK_PAUSED",
      "source": "runner",
      "symbol": None,  # 标的无关
      "detail": {
          "reason": "unresolved_live_order",
          "live_order_id": order.id,
          "trade_day": trade_day_for(market, now()),
      }
  }
  ```
- 不写入 `audit_logs`（与现有 `RISK_PAUSED` 区分；事件由 TradeEventService 负责）
- 不修改 `RiskController.pause()` 签名

**测试：**
- 事件存在（mock `record_event` 断言被调用）
- payload 完整
- `record_event` 抛异常时仍走完 pause 主流程（沿用 AuditLogger 异常吞掉模式）

### 4.3 C. 死代码最终清理

**目标：** 清理 Roadmap P8' 残留（`basedpyright` 错误清零已交付，但死代码/未引用符号未全清）。

**关键决策：**
- **扫描工具组合：**
  1. `vulture backend/app --min-confidence=80`（未引用函数/变量）
  2. `pyflakes backend/app`（未用 import）
  3. `grep -rE "Symbol|Field" backend/app/schemas.py` 配合 `grep -r "字段名" backend/app`（未用 schema 字段）
  4. 前端 `frontend/src/**/*.{ts,vue}` 用 `ts-prune` 扫未用 export
- **删除前双向 grep 确认**：每个被删符号 grep 整个 `backend/app` + `backend/tests` + `frontend/src` 确认无引用
- **删除边界：**
  - ✅ 私有函数（`_` 开头）无引用
  - ✅ 未用 import / 未用 schema 字段
  - ✅ 未用常量
  - ❌ 不删公共 API（router 中注册的 endpoint）
  - ❌ 不删 `__init__.py` 中 export（即便内部未用）
  - ❌ 不删注释 / 文档字符串
- **每批删除后跑：** `pytest` + `basedpyright` + `vue-tsc` + `build` 全绿

### 4.4 D. 测试加固

**目标：** 修复 Roadmap 546 行提到的"时区边界/偶现 flaky"风险，补充并发测试。

**关键决策：**
- **不删任何测试**（项目反模式：删除测试"修"flake）
- **加固维度：**
  1. **跨时区/夏令时边界** —— `tests/test_market_calendar.py` 增加 ET ↔ UTC ↔ HKT 三向切换边界；DST 切换日（3 月/11 月）
  2. **并发** —— 验证 `AppRunner._lock` + `RiskController` 死锁防护；注入 2 个线程同时调 `_try_buy` / `_try_sell`
  3. **flaky 修复** —— 用 `freezegun.freeze_time()` 替代依赖 `time.time()`/`datetime.now()` 的断言
  4. **mock 时间注入** —— `_recent_quotes` 时间窗、90s 静默阈值等用 `clock` 注入而非 wall clock
- **稳定性验证：** 完成后跑 `pytest -p no:randomly --count=10` 验证无 flake
- **mock 风格：** 严格遵循项目约定（inline fake classes，不引入新 mock 库）

### 4.5 E. 4 个活跃分支 rebase + 合并

**目标：** 把 4 个落后于 main 的本地分支有序合入，消除后续迭代冲突。

**关键决策：**
- **合并顺序（按 ahead 数量降序，冲突面最小化）：**
  1. `feature/addon-buy-margin-sizing`（ahead 7，rebase 14 落后）—— 主体是 P14 margin safety factor
  2. `feature/dashboard-config-performance`（ahead 1）—— 主体是 P15 Dashboard 性能
  3. `p5-plus-audit-notifications`（ahead 1）—— 主体是 P5+ 通知
  4. `refactor/maintainability-frontend`（ahead 1）—— 主体是 maintainability refactor
- **每分支流程：**
  1. `git checkout {branch}`
  2. `git rebase main`（处理冲突）
  3. `git rebase -i HEAD~N` squash 为单 commit（保留主题 commit message）
  4. `git checkout main && git merge --no-ff {branch}`
  5. 跑 `pytest` + `basedpyright` + `vue-tsc` + `build` + Cypress 全绿
  6. 下一个分支
- **冲突解决原则：**
  - 手工解（不强制 `--theirs` / `--ours`）
  - 冲突点升级到用户
  - 涉及 spec 范围外功能时拒绝合入
- **main 冻结期：** E 期间 main 不合入新功能代码（Wave 1+2 合入必须在 E 之前完成）

**前置依赖：** Wave 1+2 已合入 main 且全绿；`origin/main` 与 `main` 同步。

### 4.6 F. 前端 ai-slop 清理

**目标：** 系统化清理前端代码异味，不改变功能。

**关键决策：**
- **调用工具：** `ai-slop-remover` 技能（`/remove-ai-slops` 命令）逐文件扫描
- **异味类型：**
  1. 重复组件逻辑（≥2 处类似代码块 → 提取 composable）
  2. 魔术字符串（硬编码 status、event_type 字符串 → 提取常量）
  3. 缺失 `data-testid`（关键交互元素）
  4. 未使用 ref / computed
  5. 过度复杂 `<script setup>`（> 200 行考虑拆分）
  6. Vue 3 反模式（mutating props / 在 setup 外修改 reactive state）
- **边界：**
  - ✅ 重命名、提取、删除未用代码
  - ❌ 不重写组件逻辑
  - ❌ 不修改 props / events / 状态结构
  - ❌ 不修改 UI 视觉
- **每文件流程：** ai-slop-remover 输出 diff → 人工 review → 确认无功能变化 → 落盘
- **验证：** 现有 80+ Cypress spec 全部通过

### 4.7 P23. 前端实时通知中心 · Toast 浮层

**目标：** Dashboard 通过 Element Plus `ElNotification` / `ElMessage` 实时显示风控/跳过/审计事件，解决"必须刷新才能看到"痛点。

**关键决策：**
- **新增 composable：** `frontend/src/composables/useNotificationStream.ts`
  - 复用现有 `useStatusStream` 的 WS 连接（**不开新连接**）
  - 解析 WS 消息中的 `trade_event` / `audit_log` 类型
  - 按 `severity` 字段分级：
    | severity | 组件 | 位置 | 持续 | 声音 |
    |----------|------|------|------|------|
    | CRITICAL | `ElNotification` | top-right | 0（不自动关） | ✓ |
    | WARNING  | `ElNotification` | bottom-right | 4000ms | ✗ |
    | INFO     | `ElMessage` | top | 2000ms | ✗ |
  - **节流：** 同 `(type, detail_hash)` 1s 内不重复
  - **用户偏好（localStorage 持久化）：**
    - `notification.sound_enabled`（默认 `true`）
    - `notification.critical_persist_max_per_minute`（默认 5）
  - **断线补齐：** WS 重连后用 `GET /api/events?source=all&limit=20` 拉最近事件补齐
- **Dashboard 集成：**
  - `App.vue` 或 `Dashboard.vue` 顶层 `useNotificationStream().enable()`
  - 设置面板新增"通知偏好"开关（声音、CRITICAL 持久化）
- **不修改：**
  - `useStatusStream`（已就位）
  - `DecisionTimeline.vue`（已就位，独立视图）
  - `TradeEventService` 写路径
  - WebSocket 服务端

**测试：**
- 单测：事件分级映射、节流、用户偏好、CRITICAL 持久化上限
- Cypress：mock WS 注入 4 种 severity 事件 → 断言 4 种渲染
- 注入 100 条/秒同事件 → 断言节流生效
- 注入 WS 断线 → 断言 5s 内补齐

---

## 5. 数据流（关键路径）

### 5.1 A. SDK disconnect → 重订

```
longport QuoteContext (网络抖动/凭证失效)
  │
  ▼
_on_disconnect(reason)                  ← 新增 hook（如 SDK 暴露）
  │                                       否则 90s 看门狗兜底
  ▼
AppRunner._on_disconnect(reason)
  │
  ├─ logger.warning("broker_disconnect", extra={reason})
  ├─ audit_log("BROKER_DISCONNECT", severity="WARNING", reason)
  ├─ _quote_subscribed = False
  │
  ▼
AppRunner._run_loop (next tick, ≤5s)
  │
  ├─ if _quote_subscribed == False:
  │     ├─ unsubscribe_all()
  │     ├─ subscribe(symbols)
  │     └─ _last_quote_at = now()
  │
  └─ else: 正常 5s 循环
```

### 5.2 B. RISK_PAUSED 事件补写

```
AppRunner._run_loop (每 5s)
  │
  ├─ if _pause_if_unresolved_live_order_exists():   ← 现有
  │     │
  │     ├─ risk.pause(reason="unresolved_live_order")  ← 现有
  │     ├─ logger.warning(...)                          ← 现有
  │     │
  │     └─ TradeEventService.record_event(              ← 新增
  │            event_type="RISK_PAUSED",
  │            source="runner",
  │            symbol=None,
  │            detail={"reason": "unresolved_live_order",
  │                    "live_order_id": order.id,
  │                    "trade_day": trade_day_for(market, now())}
  │         )
```

### 5.3 P23. WS → Toast 触发

```
ws://host/ws  (现有连接，useStatusStream 已订阅)
  │
  ▼ onmessage(JSON)
  │
useNotificationStream.ts  (新 composable)
  │
  ├─ 解析 { type: "trade_event" | "audit_log", severity, ... }
  │
  ├─ severity 映射：
  │     CRITICAL → ElNotification top-right, duration=0, sound=true
  │     WARNING  → ElNotification bottom-right, duration=4000
  │     INFO     → ElMessage top, duration=2000
  │
  ├─ throttle: same (type, detail_hash) ≤ 1s 不重复
  │
  ├─ 持久化上限: CRITICAL ≤ 5 条/分钟
  │
  └─ 用户偏好 (localStorage):
        notification.sound_enabled (default true)
        notification.critical_persist (default true)

断线补齐：
  WS reconnect →
  GET /api/events?source=all&limit=20 →
  解析 + 渲染（不节流）
```

---

## 6. 错误处理与韧性

| 场景 | 处理 | 测试覆盖 |
|------|------|---------|
| A. SDK 无 disconnect 事件 | 看门狗兜底（90s 静默重订），降级运行不报错 | fake broker 模拟 60s 无 quote |
| A. 重订连续失败 ≥3 次 | 写 `BROKER_RETRY_EXHAUSTED` 审计，**不**自动 pause | 注入连续失败 |
| B. TradeEventService 写失败 | 沿用 `AuditLogger` 异常吞掉模式（不阻塞 pause） | mock 抛异常仍走完 pause |
| C. 死代码误删 | 删除前 grep 双向确认；删除后全测；git reflog 兜底 | 删除后全绿 |
| D. 测试加固引入不稳定 | 严格 mock 注入时间；不引入 `time.sleep` | 跑 `--count=10` 稳定 |
| E. 分支 rebase 冲突 | 冲突点手工解（不强制 `--theirs`/`--ours`）；冲突解完跑全测 | 每个 squash commit 后全测 |
| F. ai-slop 误改 | 每文件 diff 人工 review；不修改 props/refs/事件流 | review 每个文件 diff |
| P23. WS 断线 | 沿用 `useStatusStream` 重连逻辑；不引入新重连层 | inject 断线 → 5s 内恢复 |
| P23. 通知风暴 | 同 (type, detail) 1s 节流；CRITICAL 持久化上限 5 条/分钟 | 注入 100 条/秒同事件 |
| P23. WS 重连后事件丢失 | 拉最近 20 条补齐 | 注入断线 → 断言补齐 |

---

## 7. 测试策略

| 任务 | 测试类型 | 数量目标 | 关键场景 |
|------|---------|---------|---------|
| A | 单测 + 集成 | +5~8 | 回调触发重订、看门狗兜底、连续失败审计 |
| B | 单测 | +3 | RISK_PAUSED 事件存在、payload 完整、TradeEventService 失败仍 pause |
| C | 隐式 | 0 新增 | 死代码删除后 `pytest` + `basedpyright` 0/0/0 即可证明 |
| D | 单测加固 | +5~10 | 跨时区、并发死锁、flaky 修复、mock 时间 |
| E | 隐式 | 0 新增 | 分支合并后所有质量门禁全绿 |
| F | 隐式 | 0 新增 | ai-slop 清理后所有现有 Cypress spec 仍通过 |
| P23 | 单测 + Cypress | +8~12 | 事件分级映射、节流、用户偏好、WS 断线重连、UI 渲染 4 状态 |

**测试纪律（项目约定）：**
- pytest 9 + pytest-asyncio 0.24+（`asyncio_mode=auto`）
- 无 unittest.TestCase
- 单测用 inline fake classes（`_FakeBroker` / `_FakeSession`），不用 unittest.mock.patch
- DB 隔离：每个模块设独立 `AUTO_TRADE_DATABASE_URL`
- 总覆盖率 ≥ 80%（项目约定）

**质量门禁（每波结束必跑）：**

```bash
# 后端
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
# 前端
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

全部 exit 0 才算该波完成。

---

## 8. 风险与缓解

| ID | 风险 | 概率 | 影响 | 缓解 |
|----|------|------|------|------|
| A | longport SDK 不暴露 disconnect 事件 | 中 | 低 | 看门狗兜底，文档明示；不阻塞交付 |
| A | 重订风暴（重订又断） | 低 | 中 | 连续失败 ≥3 次只审计不自动 pause |
| B | RISK_PAUSED 事件与 audit 重复 | 低 | 低 | 复用 TradeEventService，event_type 独立；不写入 audit_logs |
| C | 死代码误删引发线上崩溃 | 低 | 高 | 删除前双向 grep + 依赖分析；删除后全测；git reflog 兜底 |
| D | 时区/并发测试假修复 | 中 | 中 | 严格 mock 注入；不删测试；用 `--count=10` 验证稳定 |
| E | 4 分支 rebase 冲突面积大 | 高 | 中 | 顺序合并（ahead 多 → ahead 少）；冲突点手工解；E 期间 main 冻结新合入 |
| E | 分支合并引入新功能蔓延 | 中 | 中 | squash 为单 commit + 严格 review；不引入 spec 范围外功能 |
| F | ai-slop 误改功能 | 中 | 中 | 每文件 diff 人工 review；不修改 props/refs/事件流 |
| P23 | 通知风暴淹没主 UI | 中 | 中 | 节流 + 持久化上限 + 关闭按钮；用户偏好可全局静音 |
| P23 | WS 断线时通知丢失 | 中 | 中 | 断线重连后用 `/api/events?limit=20` 拉最近事件补齐 |
| 跨 | 3 波之间 main 漂移 | 低 | 中 | 每波结束合入前 rebase 自身到 main |
| 跨 | subagent 跨任务文件冲突 | 中 | 中 | 每个 subagent 限定文件清单；冲突点升级到主调度器 |

---

## 9. 交付物清单

### 9.1 文档

- **本 spec**：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md`
- **7 独立 plan**（与本 spec 同目录）：
  - `docs/superpowers/plans/2026-06-04-A-sdk-disconnect-callback.md`
  - `docs/superpowers/plans/2026-06-04-B-risk-paused-event.md`
  - `docs/superpowers/plans/2026-06-04-C-dead-code-cleanup.md`
  - `docs/superpowers/plans/2026-06-04-D-test-hardening.md`
  - `docs/superpowers/plans/2026-06-04-E-merge-4-branches.md`
  - `docs/superpowers/plans/2026-06-04-F-frontend-ai-slop.md`
  - `docs/superpowers/plans/2026-06-04-P23-toast-notification-center.md`
- **Roadmap 同步**：完成后 `docs/Roadmap.md` 新增「迭代 P24：技术债清扫 + P23」段，标 ✅ 状态，更新测试基线

### 9.2 代码（按波次）

- **Wave 1**：A 改 `core/broker.py` / `runner.py` + 测试；B 改 `runner.py` / `services/trade_event_service.py` + 测试
- **Wave 2**：D 加固 ~5~10 测试；C 死代码删除清单（diff）；F 前端 diff 清单
- **Wave 3**：P23 新增 `composables/useNotificationStream.ts` + 改造 `Dashboard.vue` + `App.vue` + 8~12 测试；E 完成 4 分支 rebase + squash + 合并

### 9.3 证据

- `.sisyphus/evidence/task-{id}-{slug}.txt`：每任务完成时记录 pytest/basedpyright/vue-tsc/build/cypress 输出
- 死代码清理：C 任务输出 `dead-code-candidates.txt` 候选清单 + 删除决策
- 分支合并：E 任务输出 `branch-merge-log.md` 含冲突解点 + squash commit hash

---

## 10. 验证策略

### 10.1 逐任务验证（每任务完成时）

- 单任务所有测试通过
- 全栈 `pytest` + `basedpyright` + `vue-tsc` + `build` 全绿
- 任务相关 Cypress spec 通过

### 10.2 逐波验证（每波结束）

- **Wave 1 末：** 后端 `pytest` + `basedpyright` + Cypress 全绿
- **Wave 2 末：** 同上
- **Wave 3 末：** 同上 + 4 分支全部合入 main + 浏览器手测 P23 Toast 浮层

### 10.3 最终验证（所有任务完成）

- Docker Compose 启动：`docker compose up --build -d`
- `curl -fsS http://localhost:8080/api/health` → 200
- 浏览器访问 Dashboard → 触发风控/跳过/审计事件 → 看到 Toast 浮层
- 4 review agent 并行审核：
  - **F1 Plan Compliance** —— oracle
  - **F2 Code Quality** —— unspecified-high
  - **F3 Real Manual QA** —— unspecified-high + playwright
  - **F4 Scope Fidelity** —— deep
- 等待用户显式 "okay" 后标记完成

---

## 11. 执行节奏

| 周 | 波次 | 任务 | 估时 | 累计 |
|----|------|------|------|------|
| W1 Day 1-2 | Wave 1 | A + B | 1.5~2 天 | ~2 天 |
| W1 Day 3-5 / W2 Day 1-2 | Wave 2 | D + C + F | 2~2.5 天 | ~4.5 天 |
| W2 Day 3-5 / W3 Day 1-2 | Wave 3 | P23 + E | 3~3.5 天 | ~8 天 |
| W3 Day 3 | 最终验证 | 4 review agents + 用户审 | 1 天 | ~9 天 |

**总耗时：** ~1.5~2 周（含 review 与 buffer）

**关键里程碑：**
- W1 EOD Wave 1：后端韧性合入 main
- W2 EOD Wave 2：质量清扫合入 main
- W3 mid Wave 3：4 分支全部合入 + P23 Toast 浮层就绪
- W3 EOD：所有 review 通过 + 用户最终签收

---

## 12. 显式 YAGNI 边界（最终确认）

本迭代**严格不做**：

- 节假日历、审计 CSV/JSON 导出、Webhook 模板编辑器、通知重发队列
- API 鉴权收紧、多标的自动交易
- PWA 离线支持
- 高频交易、复杂择时指标、量化研究平台
- 代客理财 / 公开策略分发
- 主动 commit、改 main 分支策略、改 CI 发布流程
- 修改 longport SDK 行为、trading session guard 语义、risk/cooldown/fee guard 现有行为
- 任务 F/P23 中重写组件、修改 props/refs/事件流

---

**文档结束。本 spec 是 7 个 plan 文件的母文档；任何 plan 实施过程中如发现与本 spec 不一致，必须先回到本 spec 修订，再修改 plan。**
