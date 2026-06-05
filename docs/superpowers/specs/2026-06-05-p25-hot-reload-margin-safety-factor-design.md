# P25: 运行时策略参数热重载 — `margin_safety_factor` 链路修复

> **日期：** 2026-06-04
> **状态：** 已交付
> **目标：** 修复 `margin_safety_factor` 从配置保存到交易执行的全链路断裂，使其支持保存后热重载与 runner 冷启动加载。

---

## 问题诊断

`margin_safety_factor` 虽然在数据库模型、API Schema、前端表单中均有定义，但存在**完整的四层级链路断裂**：

| 层级 | 当前状态 | 期望状态 |
|------|---------|---------|
| **前端** | Strategy.vue 表单可编辑、可保存 | ✅ 无需改动 |
| **API 层** | `PUT /api/strategy` 的 merged 字典**未包含** `margin_safety_factor` | 应包含并传递 |
| **服务层** | `StrategyService.update_config()` 的 `updatable_fields` **未包含** `margin_safety_factor` | 应包含并持久化 |
| **Runner 层** | `reload_strategy()` **未更新** `_trade_svc.margin_safety_factor` | 应在重载时注入 |
| **执行层** | `TradeExecutionService._entry_quantity_from_margin_power` 使用**硬编码常量** `ENTRY_BUYING_POWER_USAGE = Decimal("0.9")` | 应优先使用配置值 |

结果是：用户在前端修改 `margin_safety_factor` 并保存后，值虽然能写入请求体，但后端不接收、不持久化、不传递、最终下单时仍用硬编码 0.9。

---

## 方案选择

采用 **方案 A：TradeExecutionService 实例属性注入**。

理由：
- 改动范围最小，与现有 `reload_strategy()` 更新 `engine.params` / `risk.config` 的模式完全一致。
- 无额外数据库查询开销（对比方案 C）。
- 不需要修改 `execute()` 签名及所有调用点（对比方案 B）。

---

## 架构改动

### 1. API 层：`backend/app/api/strategy.py`

在 `PUT /api/strategy` 的 merged 字典中增加 `margin_safety_factor` 字段：

```python
merged = {
    ...,
    "margin_safety_factor": data["margin_safety_factor"] if "margin_safety_factor" in data and data["margin_safety_factor"] is not None else current.margin_safety_factor,
}
```

### 2. 服务层：`backend/app/services/strategy_service.py`

- `STRATEGY_AUDIT_KEYS` 元组追加 `"margin_safety_factor"`
- `updatable_fields` 列表追加 `"margin_safety_factor"`

### 3. Runner 层：`backend/app/runner.py`

在 `reload_strategy()` 中，更新完 `engine.params` 和 `risk.config` 后，增加：

```python
self._trade_svc.margin_safety_factor = getattr(config, "margin_safety_factor", None)
```

同时在 `AppRunner._initialize_runner()` 中从 `RuntimeStateService.load()` 返回的配置注入启动初始值，避免进程重启后回退到硬编码常量。

### 4. 执行层：`backend/app/services/trade_execution_service.py`

- `__init__` 增加 `margin_safety_factor: float | None = None` 参数，保存为实例属性。
- `_entry_quantity_from_margin_power` 改为：
  ```python
  factor = Decimal(str(safety_factor)) if safety_factor is not None else (
      Decimal(str(self.margin_safety_factor)) if self.margin_safety_factor is not None else ENTRY_BUYING_POWER_USAGE
  )
  ```
- 所有调用 `_entry_quantity_from_margin_power` 的位置**不需要**改动签名（它已有 `safety_factor` 关键字参数，外部不传入时走实例属性回退）。

---

## 数据流

```
前端 Strategy.vue 保存
  → PUT /api/strategy (payload 含 margin_safety_factor)
    → api/strategy.py merged 字典合并字段
      → StrategyService.update_config() 持久化到 strategy_config 表
        → _reload_strategy_after_save() 触发 runner.reload_strategy()
          → runner.reload_strategy() 读取最新 StrategyConfig
            → _trade_svc.margin_safety_factor = config.margin_safety_factor
              → 下次 _on_quote() → execute() → _entry_quantity_from_margin_power()
                → 使用新 factor 计算下单量
```

---

## 测试策略

### 后端测试

1. **API 层测试**：`test_strategy_api.py` 补充 `margin_safety_factor` 在 PUT 请求中的传递与响应验证。
2. **服务层测试**：`test_strategy_service.py` 补充 `update_config` 对 `margin_safety_factor` 的持久化验证。
3. **Runner 层测试**：`test_runner.py` 补充 `reload_strategy()` 后 `_trade_svc.margin_safety_factor` 被更新的断言。
4. **执行层测试**：`test_trade_execution_service.py` 补充：
   - `_entry_quantity_from_margin_power` 优先使用 `self.margin_safety_factor`
   - `safety_factor` 关键字参数仍可覆盖实例属性
   - 实例属性为 None 时回退到 `ENTRY_BUYING_POWER_USAGE`

5. **冷启动测试**：`test_runner.py` 补充 `_initialize_runner()` 加载 DB 配置后 `_trade_svc.margin_safety_factor` 被注入的断言。

### 验证命令

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
```

### 2026-06-04 验证结果

- `python -m pytest tests/test_trade_execution_service.py::TestTradeExecutionServiceBasics::test_margin_safety_factor_instance_attribute ... tests/test_api.py::TestAPI::test_update_strategy_persists_margin_safety_factor -v`：8 passed。
- `python -m pytest tests/ -v`：759 passed。
- `basedpyright`：0 errors / 0 warnings / 0 notes。

---

## 显式不做

- 不涉及前端改动（表单、API 调用均已支持）。
- 不涉及 `auto_interval_enabled` / `llm_interval_minutes`（已在 main.py cron 中每次从 DB 读取，自然热重载）。
- 不涉及其他未使用的策略参数（本次仅修复已发现断裂的 `margin_safety_factor`）。
- 不涉及数据库迁移（`margin_safety_factor` 列已存在）。

---

## 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| `reload_strategy()` 中增加字段后，若 `config.margin_safety_factor` 为 None（旧数据），可能未正确回退 | 使用 `getattr(config, "margin_safety_factor", None)` + 执行层回退到常量 |
| `TradeExecutionService` 新增实例属性，测试中的 fake/mock 对象可能未提供 | 更新测试中 fake 对象的构造，或给属性默认值 None |
| 并发：reload 线程与交易循环同时执行 | 现有 `_state_lock` 已保护 reload；`_entry_quantity_from_margin_power` 读取实例属性是原子操作（Python GIL 下单属性赋值是原子的） |

回滚策略：还原 4 个文件的改动即可；数据库中 `margin_safety_factor` 列不受影响。
