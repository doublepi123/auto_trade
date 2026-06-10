# Review-Fix Loop 报告

- **目标**: 整个仓库（~44,930 行，62 个源文件）
- **时间**: 2026-06-10
- **总迭代次数**: 1（7 分片并行 + 全局收敛）
- **终止原因**: 收敛完成，2 个改动被回退（行为不符合项目设计）
- **是否分片并行**: yes（7 个 shard）
- **Baseline SHA256**: `d19e38168e44e13fa695bda03685f40d3cde401c254f480e257c1db742bdefde`

## Shard Plan

| Shard | 范围 | 文件数 |
|-------|------|--------|
| A | Trading Core（engine, risk, runner） | 5 |
| B | Broker & Data Infrastructure | 8 |
| C | Services Layer | 12 |
| D | API Layer & Config | 20 |
| E | LLM & Domain Logic | 16 |
| F | Frontend | ~30 |
| G | Tests & Infrastructure | ~50 |

## 迭代历史

### Iteration 1 — Review（7 个并行 review agent）

**发现 121 个问题**，按 shard 分布：

| Shard | 状态 | 问题数 | 关键发现 |
|-------|------|--------|----------|
| A: Trading Core | issues_found | ~12 | runner 直接访问 engine._lock；market_calendar 时区边界 |
| B: Broker & Data | issues_found | 19 | retry 未覆盖网络异常；RSI/ATR 零值回退不可区分；Bollinger pstdev vs stdev |
| C: Services | issues_found | 20 | pending order 覆盖；_pending_order setter 清空所有订单；reconcile 竞态 |
| D: API & Config | issues_found | 29 | WebSocket 无认证；写端点缺 auth；输入校验缺口；Content-Disposition 文件名注入 |
| E: LLM & Domain | issues_found | 13 | prompt injection via A/B template；guardrail 除零；EMA seeding 不匹配 |
| F: Frontend | issues_found | 23 | API 返回类型不一致；轮询间隔过激；`any` 类型滥用 |
| G: Tests & Infra | issues_found | 17 | 测试隔离差（per-module DB URL 覆盖）；pyrightconfig typeCheckingMode=off |

### Iteration 1 — Fix（6 个并行 fix agent）

**修复了 ~100 个问题**，涉及 62 个文件，净增 643 行、删 481 行。

### Iteration 1 — 全局收敛 Review

**发现 5 个问题**：
1. `test_runner.py` shard 修复引入回归 → 已验证实际通过
2. `DiagnosticSymbolRuntime` 缺少 `quote_quality` 字段
3. 前端 `LLMIntervalStatus` 缺少 `budget`/`symbol_statuses`
4. 2 个预存测试失败（缺 `init_db()`）

### 主动回退的改动

以下 2 个改动经人工审查后回退，因为与项目设计冲突：

1. **`docker-compose.yaml`**: 默认绑定从 `0.0.0.0` 改为 `127.0.0.1` → 破坏内网 LAN 访问（CLAUDE.md: "仅内网使用"）
2. **`backend/app/api/ws.py`**: 新增 WebSocket 认证 → 当 `api_key` 为空时在生产环境拒绝连接，与 "API_KEY 可留空" 矛盾

## 最终状态

- **所有发现的问题**: 126（121 shard + 5 convergence）
- **已修复**: ~100（应用到代码库）
- **主动回退**: 2（与项目设计冲突）
- **剩余未修复**: ~24（低优先级或需要更大重构）
- **循环检测触发**: no
- **测试结果**: 777 tests, 776 passed, 1 skipped, 0 failed

## 已应用的关键修复

### 高优先级（High）
- **trade_execution_service.py**: pending order 覆盖保护；`_pending_order` setter 不再清空所有订单
- **llm_advisor_service.py**: prompt injection 防护（SystemModule role instructions 始终前置）
- **broker.py**: retry 逻辑扩展到覆盖网络异常
- **technical_indicators.py**: RSI/ATR 零值回退现在可区分无效读数
- **data_aggregator.py**: Bollinger Bands 使用 pstdev（总体标准差）替代 stdev

### 中优先级（Medium）
- **API 层**: 多个端点添加 `require_api_key` 依赖
- **credentials.py**: GET /credentials 添加认证
- **event_list_service.py**: 分页 total 计算修复
- **review_service.py**: 审计 diff 修复
- **strategy_experiment_service.py**: 实验激活竞态修复
- **测试隔离**: 移除 per-module DB URL 覆盖，统一使用 conftest PID-based URL

### 低优先级（Low）
- **前端**: `useNotificationStream` LRU 淘汰改用 timestamp
- **前端**: 多个 view 组件类型安全改进
- **CI**: basedpyright 指定 pyrightconfig.json 路径
- **config.py**: 新增 `AUTO_TRADE_FRONTEND_BIND` 配置项（文档用）

## 最终建议

1. **剩余 24 个低优先级问题**可在后续迭代中处理，不阻塞当前功能
2. **收敛发现的 schema drift**（DiagnosticSymbolRuntime.quote_quality、LLMIntervalStatus.budget）需在下一迭代中补齐
3. **WebSocket 认证**建议作为独立功能实现，需重新评估 intranet 场景下的认证策略
4. **pyrightconfig.json** 的 `typeCheckingMode=off` 建议改为 `basic` 以启用 CI 类型检查
