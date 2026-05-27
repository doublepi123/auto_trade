# P8 多标的观察列表（暂不自动交易）Implementation Plan

> **For agentic workers:** 严格遵守"观察为主，不改交易引擎"原则。

**Goal:** 支持多个标的的行情观察和 LLM 建议查看，但**暂不允许多标的自动下单**。

**Architecture:** 
- 新增 `WatchlistItem` 表：仅保存观察标的列表
- 现有 `StrategyConfig.symbol` 继续作为**唯一交易标的**
- `AppRunner` / `StrategyEngine` **零改动**——仍只订阅和交易单标的
- Watchlist 标的通过独立 API 获取行情和 LLM 建议，不进入交易循环

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 / SQLite / pytest / Vue 3 + TypeScript + Element Plus / Cypress

**Baseline (2026-05-28):** `pytest 493 passed`，`basedpyright` 0 errors / 0 warnings，`npm run type-check` + `npm run build` 通过。

**Estimated Effort:** 4–5 天

---

## 迭代目标 (Sprint Goal)

> 用户可以在观察列表页面添加多个标的，查看它们的实时价格和 LLM 建议；只能将其中一个设为"交易标的"，切换前需确认；Dashboard 显示观察列表摘要。

**完成定义 (Definition of Done):**
1. `watchlist_items` 表 + CRUD API + runtime migration
2. `GET /api/watchlist` 返回带最新行情的观察列表
3. `POST /api/watchlist/:symbol/set-trading` 切换交易标的（带确认弹窗）
4. `POST /api/watchlist/:symbol/analyze` 对观察标的执行 LLM 预览分析
5. 前端 Watchlist.vue：表格、添加/删除、设为交易标的按钮、LLM 分析按钮
6. Dashboard.vue 增加观察列表摘要卡片
7. `pytest` 新增 ≥10 项，`npm run type-check` + `npm run build` 通过

---

## Task 拆分

### T1：数据模型 + 迁移 (0.5 天)

**Files:**
- Modify: `backend/app/models.py`（`WatchlistItem`）
- Modify: `backend/app/database.py`（`_ensure_watchlist_table`）
- Modify: `backend/app/schemas.py`（`WatchlistItemSchema`）
- Test: `backend/tests/test_database.py`

```python
class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    market: Mapped[str] = mapped_column(String(10), default="US")
    is_active_trading: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(_TZDateTime(), default=_utcnow)
```

**约束：**
- `symbol` 必须含市场后缀（通过 schema validator）
- `is_active_trading=True` 的 item 最多只有一个（数据库级别用 partial index，SQLite 不支持则用应用层检查）

---

### T2：后端 API (1.5 天)

**Files:**
- Create: `backend/app/services/watchlist_service.py`
- Create: `backend/app/api/watchlist.py`
- Modify: `backend/app/main.py`（mount router）
- Test: `backend/tests/test_watchlist.py`

#### T2.1 — WatchlistService

```python
class WatchlistService:
    def list_items(self) -> list[WatchlistItem]
    def add_item(self, symbol: str, market: str) -> WatchlistItem  # 校验 symbol 格式
    def remove_item(self, symbol: str) -> None
    def set_trading_symbol(self, symbol: str) -> WatchlistItem  # 校验 symbol 在 watchlist 中
    def get_trading_symbol(self) -> str | None
```

**`set_trading_symbol` 行为：**
1. 将指定 symbol 设为 `is_active_trading=True`
2. 将所有其他 item 设为 `is_active_trading=False`
3. **同步更新 `StrategyConfig.symbol`**（这样现有交易引擎无需改动）
4. 如果 runner 正在运行，记录 warning（不自动重启，避免中断交易）

#### T2.2 — API 路由

```python
@router.get("/api/watchlist")
def get_watchlist(db: Session = Depends(get_db)) -> list[WatchlistItemOut]

@router.post("/api/watchlist")
def add_watchlist_item(payload: WatchlistItemIn, db: Session = Depends(get_db)) -> WatchlistItemOut

@router.delete("/api/watchlist/{symbol}")
def remove_watchlist_item(symbol: str, db: Session = Depends(get_db)) -> MessageResponse

@router.post("/api/watchlist/{symbol}/set-trading")
def set_trading_symbol(symbol: str, db: Session = Depends(get_db)) -> StrategyConfigSchema
    # 同时更新 StrategyConfig.symbol

@router.post("/api/watchlist/{symbol}/analyze")
def analyze_watchlist_item(symbol: str, db: Session = Depends(get_db)) -> LLMPreviewResponse
    # 复用现有 previewLLMInterval 逻辑，但仅分析不交易
```

#### T2.3 — 带行情的观察列表

`GET /api/watchlist` 可选带 `?with_quotes=1`，此时：
1. 查询 watchlist_items
2. 对每个 symbol 调用 `broker.get_quote(symbol)`（批量调用 `get_quotes([symbols])` 如果有）
3. 返回 `last_price`、`updated_at`

**注意：** broker 调用失败时返回 `price: null`，不阻塞列表返回。

---

### T3：前端 — Watchlist.vue (1.5 天)

**Files:**
- Create: `frontend/src/views/Watchlist.vue`
- Create: `frontend/src/api/watchlist.ts`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/App.vue`

#### T3.1 — 页面布局

```
┌─────────────────────────────────────────┐
│ 观察列表                                │
├─────────────────────────────────────────┤
│ [股票代码 ▼] [添加]                     │
├─────────────────────────────────────────┤
│ 标的      市场    最新价    LLM建议    操作 │
│ AAPL.US   US      $225.50   [分析]   [设为交易] [删除] │
│ NVDA.US   US      $142.30   [分析]   [设为交易] [删除] │
│ 0700.HK   HK      $385.20   [分析]   [设为交易] [删除] │
├─────────────────────────────────────────┤
│ ⚠️ 当前交易标的：AAPL.US（切换需确认）      │
└─────────────────────────────────────────┘
```

#### T3.2 — "设为交易标的" 交互

- 点击后弹出 `ElMessageBox.confirm`："确定将交易标的切换为 NVDA.US 吗？当前策略配置和 LLM 区间将针对新标的重置。"
- 确认后调用 `POST /api/watchlist/{symbol}/set-trading`
- 成功提示后刷新 Strategy 页面数据（如果用户正在看 Strategy 页）

#### T3.3 — LLM 分析按钮

- 点击后调用 `POST /api/watchlist/{symbol}/analyze`
- 结果以 `el-dialog` 展示建议区间和置信度
- **明确标注"仅预览，不会自动交易"**

---

### T4：Dashboard 集成 (0.5 天)

**Files:**
- Modify: `frontend/src/views/Dashboard.vue`

在 Dashboard  cockpit-grid 下方增加一个小的观察列表摘要卡片：
- 显示 3–5 个观察标的的最新价格（从 watchlist API 拉取）
- 点击可跳转到 Watchlist 页面
- 标记当前交易标的（绿色边框或 tag）

---

### T5：测试 (1 天)

- `test_watchlist.py` ≥10 项：
  - 添加/删除 item
  - 重复添加同一 symbol 报错
  - `set_trading_symbol` 更新 StrategyConfig
  - `is_active_trading` 唯一性
  - 删除当前交易标的后 `StrategyConfig.symbol` 不变（或清空？需定义）
  - 带 quotes 的列表返回
- Cypress：
  - `watchlist.cy.ts`：添加标的、删除标的、切换交易标的确认弹窗

---

## 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| `StrategyConfig.symbol` 与 `WatchlistItem.is_active_trading` 不同步 | 交易引擎跑错标的 | `set_trading_symbol` 必须原子更新两者；测试覆盖 |
| Watchlist 行情查询拖慢 API | 前端加载慢 | 默认不带 quotes，前端单独轮询；或用 `get_quotes` 批量 |
| 用户误删当前交易标的 | 引擎断行情 | 删除时校验：若 `is_active_trading=True`，需二次确认并提示"删除后交易标的将失效" |
| 多标的需求膨胀为"同时交易多标的" | 架构重构风险 | **P8 计划文档和 UI 明确标注"暂不自动交易"**；Roadmap 中 P8 后无多标交易计划 |

---

## 关键决策记录

### 为什么不直接改 StrategyEngine 支持多标的？

`StrategyEngine` 是 flat/long/short 状态机，单标的有以下深层绑定：
1. `params.symbol` 只有一个
2. `broker.subscribe_quotes(symbol, callback)` 只订阅一个
3. `_last_llm_action_at[(symbol, side)]` 虽然可扩展，但引擎 `update_price` 和 `trigger` 不区分 symbol
4. `TrackedEntry` 以 `symbol` 为 PK，单标的没问题，多标的需重写对账逻辑
5. 风控 `daily_pnl` 目前也不分 symbol

结论：**P8 观察列表与交易引擎解耦**，通过 `StrategyConfig.symbol` 作为桥梁。未来若要做多标的交易，需单独立项（P9+），不是 P8 的范围。

### SQLite 唯一 active trading 约束

SQLite 不支持 partial unique index，因此在 `WatchlistService.set_trading_symbol` 中：
```python
with db.begin():
    db.query(WatchlistItem).update({"is_active_trading": False})
    item.is_active_trading = True
```
用事务保证原子性。
