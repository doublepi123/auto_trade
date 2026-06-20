# Review-Fix Loop 报告

- **目标**: 整个项目 (`backend/app` + `frontend/src`)
- **时间**: 2026-06-11
- **总迭代次数**: 4
- **终止原因**: clean（剩余问题为低优先级架构/部署限制，见最终建议）

## 迭代历史

### Iteration 1
**Review**: 发现 12 个问题

- [critical] `credentials.py:60` — PUT 凭证无鉴权
- [high] `trade.py:353` — 撤单无鉴权
- [high] `client.ts` — 前端未发送 X-API-Key
- [medium] `runner.py` / `llm_advisor.py` — `cast(str | None)` 在 Python 3.9 运行时崩溃
- [medium] `runner.py:644` — 未知 symbol 静默回退到主标的
- [medium] `runner.py:1391` — 全局 `has_pending_order` 阻塞主标的仓位同步
- [medium] `trade_execution_service.py:805` — `_track_pending_order` 异常未捕获
- [medium] `auth.py:29` — dev 环境在配置了 API key 时仍绕过鉴权
- [low] `main.py:148` — WebSocket 清理异常被静默吞掉
- [low] `ws.py:72` — WebSocket 无鉴权

**Fix**: 修复了 10 个问题
- 凭证 PUT、撤单加 `require_api_key()`
- 配置 API key 后强制校验（移除 dev 无 header 绕过）
- 前端 `VITE_AUTO_TRADE_API_KEY` axios 拦截器
- `cast()` 改为 `Optional[...]`；`_runtime_for_symbol` 返回 `None` + `UNKNOWN_SYMBOL`
- 仓位同步改为 per-symbol `pending_order_for()`
- `_track_pending_order` 包裹 `OrderPersistenceError` 恢复
- WebSocket 清理加 `logger.exception`
- 测试：`test_main.py` 加 future annotations；更新 preview 鉴权测试

**结果**: 821 passed（从 28 failed 恢复）

### Iteration 2
**Review**: 发现 8 个问题

- [high] `credential_crypto.py` — 无 master key 时明文写 PEM
- [high] `ws.py` — WebSocket 无鉴权
- [medium] `backtest.py` — 回测无鉴权
- [medium] `trade.py:509` — 账户接口无鉴权
- [medium] `runner.py` — `_execute_llm_trade_action` / `_quote_for_llm_order` / `_precheck_llm_action` 未处理 `None`
- [low] `test_runner.py` — 缺 future annotations

**Fix**: 修复了 7 个问题
- `backtest/run`、`/account` 加鉴权
- WebSocket `?api_key=` 校验 + 前端 `useStatusStream` 传 key
- `credential_crypto` 非 dev/test 无 master key 时 fail-fast
- runner 三处 `None` guard
- `test_runner.py` 加 future annotations

**结果**: 821 passed

### Iteration 3
**Review**: 发现 5 个问题

- [high] Docker 构建未注入 `VITE_AUTO_TRADE_API_KEY`
- [high] WebSocket 在 prod 无 key 时仍开放
- [medium] WS query string 泄露 key（已知权衡）
- [medium] `GET /orders` 可触发 broker sync 无鉴权
- [medium] 部分读接口仍无鉴权

**Fix**: 修复了 4 个问题
- `frontend/Dockerfile` ARG + `docker-compose.yaml` build-arg
- WebSocket prod 无 key 时拒绝连接
- `GET /orders` 加鉴权

**结果**: 821 passed

### Iteration 4
**Review**: 继续收紧读接口鉴权

**Fix**:
- `GET /status`、`/diagnostics`、`/status/history` 加鉴权
- `GET /events`、`/events/export` 加鉴权
- `GET /watchlist/snapshots`、`/watchlist/quotes` 加鉴权
- `test_deploy_config.py` 新增 Docker build-arg 回归测试

**结果**: 823 passed, 1 skipped

## 最终状态

- **所有发现的问题**: ~25
- **已修复**: ~22
- **无法在本轮安全修复**: 3
- **循环检测触发**: no

## 最终建议（需手动关注）

1. **多标的仓位同步**: `_sync_engine_state_with_positions()` 仍只同步主 engine；secondary watchlist 引擎在重启/手动交易后可能漂移。需单独迭代扩展为多标的循环同步。
2. **Docker Hub 预构建镜像**: 当前版本由 nginx 在服务端注入 `X-API-Key`，不再需要也不应通过 `VITE_AUTO_TRADE_API_KEY` 把密钥写入前端 bundle。
3. **WebSocket 鉴权方式**: 当前使用 `?api_key=` query param，可能被反向代理日志记录。若部署在不可信网络，可后续改为握手后首条 auth 消息或短期 ticket。
4. **本地开发**: 若设置了 `AUTO_TRADE_API_KEY`，从同一 shell 启动 Vite，代理会在服务端注入 `X-API-Key`，浏览器端不需要密钥。

## 验证

```
cd backend && python3 -m pytest tests/ -q   # 823 passed, 1 skipped
cd frontend && npm run type-check           # clean
```
