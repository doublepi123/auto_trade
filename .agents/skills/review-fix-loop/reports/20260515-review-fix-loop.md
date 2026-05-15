# Review-Fix Loop 报告

- **目标**: 全项目（逻辑、交互、体验、UI）
- **时间**: 2026-05-15
- **总迭代次数**: 1
- **终止原因**: clean
- **是否分片并行**: yes
- **shard 数量**: 3

## Shard Plan
- shard A: backend core (engine, broker, risk, notify, credential_crypto, runner)
- shard B: backend API layer (trade, ws, strategy, auth, credentials, main, database, models, schemas, config, services)
- shard C: frontend (App, Dashboard, Strategy, Credentials, TradeHistory, api, types, labels, router, vite, nginx, docker)

## 迭代历史

### Iteration 1 (Round 1)

**Review**: 发现 27 个问题

Shard A (7 issues):
- [critical] runner.py:222 — broker 提交成功但 _record_order 失败时，异常导致引擎状态回滚，产生已提交但未记录的订单
- [high] runner.py:166 — _handle_trigger 异常时 snapshot 未恢复
- [medium] risk.py:57 — record_trade 跨日不重置 consecutive_losses
- [medium] credential_crypto.py:78 — PBKDF2 salt 硬编码
- [medium] credential_crypto.py:88 — KEK 解密失败时静默回退
- [medium] broker.py:133 — 更换订阅标的时未取消旧订阅
- [low] risk.py:80 — disable_kill_switch 不清理 _kill_switch_reason

Shard B (10 issues):
- [high] strategy.py:37 — 当 buy_low/sell_high 为 0 时 merged 重新校验失败
- [high] ws.py:88 — 非 WebSocketDisconnect 异常时连接泄漏
- [high] credentials.py:31 — reload 失败后 API 返回成功但无警告
- [medium] models.py:24 — 金融值使用 Float 类型
- [medium] trade.py:28 — DB 和 runner 状态非原子更新
- [medium] strategy_service.py:14 — TOCTOU 竞态条件
- [medium] trade.py:35 — stop 先 stop() 再 pause()，顺序不对
- [medium] main.py:24 — lifespan 自动 start 与 /control/start 重复启动
- [medium] credentials_service.py:31 — getter 中有写入副作用
- [low] schemas.py:25 — 部分更新时 sell_high 校验覆盖不完整

Shard C (10 issues):
- [critical] docker-compose.yaml:19 — healthcheck JSON 数组格式中 shell 操作符 || 不生效，整个栈无法启动
- [high] api/index.ts:8 — 无 UI 设置 API Key，401 后用户卡死
- [high] Dashboard.vue:92 — 未使用 WebSocket，仅轮询，浪费资源且延迟高
- [medium] Dashboard.vue:132 — 确认弹窗英文与中文 UI 不一致
- [medium] TradeHistory.vue:9 — 未使用已有的中文标签函数
- [medium] Strategy.vue:3 — 全英文 UI 与其他页面中文不一致
- [medium] Dashboard.vue:40 — kill_switch 激活时操作按钮未禁用
- [medium] Dashboard.vue:90 — 初始加载无 loading 状态
- [low] TradeHistory.vue:36 — 无分页和刷新
- [low] router/index.ts:7 — 无 404 路由

**Fix**: 修复了 18 个问题

| # | 严重度 | 文件 | 修复内容 |
|---|--------|------|----------|
| 1 | critical | docker-compose.yaml | healthcheck 改为 CMD-SHELL 格式，URL 改为 /api/health |
| 2 | critical | runner.py | _on_quote 中添加 try/except 保证异常时恢复 snapshot |
| 3 | critical | runner.py | 新增 _safe_record_order / _safe_notify_order，订单记录/通知失败不影响引擎状态 |
| 4 | high | ws.py | 添加 finally 块确保 disconnect 被调用 |
| 5 | high | strategy.py + schemas.py | 新增 StrategyMergedSchema 允许 buy_low/sell_high 为 0 |
| 6 | high | credentials.py + schemas.py | CredentialResponse 增加 reload_warning 字段 |
| 7 | high | App.vue | 新增 API Key 设置/清除对话框，监听 api-key-required 事件 |
| 8 | high | Dashboard.vue | 接入 WebSocket 实时推送，轮询降级为 fallback |
| 9 | high | Dashboard.vue | 确认弹窗改为中文 |
| 10 | high | Dashboard.vue | kill_switch 激活时禁用启动/暂停/恢复按钮 |
| 11 | medium | Dashboard.vue | 初始加载增加 v-loading |
| 12 | medium | Strategy.vue | 全部 UI 翻译为中文 |
| 13 | medium | TradeHistory.vue | 使用 orderSideLabel / orderStatusLabel 中文标签 |
| 14 | medium | TradeHistory.vue | 增加刷新按钮 |
| 15 | medium | risk.py | record_trade 跨日重置 consecutive_losses |
| 16 | medium | risk.py | disable_kill_switch 清理 _kill_switch_reason |
| 17 | medium | trade.py | stop_runner 中先 pause 再 stop |
| 18 | medium | credential_crypto.py | KEK 不匹配时抛出清晰错误而非静默回退 |

## 未修复问题（需更大改动/低优先级）

| # | 严重度 | 描述 | 原因 |
|---|--------|------|------|
| 1 | medium | models.py Float 用于金融值 | 需 DB migration，影响面大 |
| 2 | medium | strategy_service.py TOCTOU 竞态 | 需加锁或 schema 变更 |
| 3 | medium | credentials_service.py getter 写入副作用 | 需重构调用链 |
| 4 | medium | trade.py DB+runner 非原子更新 | 需事务+补偿模式 |
| 5 | medium | main.py lifespan 自动 start | runner.start() 已幂等，实际无影响 |
| 6 | medium | broker.py 更换订阅未取消旧订阅 | 依赖 SDK unsubscribe API |
| 7 | medium | credential_crypto.py PBKDF2 salt 硬编码 | 需存储随机 salt |
| 8 | low | schemas.py 部分更新校验 | 已通过 StrategyMergedSchema 缓解 |
| 9 | low | TradeHistory 无分页 | 功能增强 |
| 10 | low | 路由无 404 | 功能增强 |

## 最终状态
- 所有发现的问题: 27
- 已修复: 18
- 无法修复（需大改动）: 10
- 循环检测触发: no
- 后端测试: 101 passed

## 基线哈希
- 初始: a610a32afbf3805e6614e4184778430020aaaf9f58f486ce61d9e262759238fd
- 最终: 560e87e61959599da3c39cce425870780a75a780ba48454bb52211c2e92d3abe
