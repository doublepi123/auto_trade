# P42 Round 13 — Security-focused review

> 来源：1 个 Explore subagent 跑 security 扫描。
> 工作区：`/home/lcy/code/auto_trade-p42`

## 整体结论

代码库 security 实践**整体稳健**，对可信内网部署是充分的：

- ✅ 全部 21 个写端点（control×6 / strategy / credentials / orders cancel / watchlist×4 / experiments×2 / strategy_experiments×2 / llm_advisor×4 / backtest）都有 `Depends(require_api_key())`。
- ✅ `require_api_key()` 用 `secrets.compare_digest()` 时间安全比较，失败日志只记 client IP 不记 key。
- ✅ `CREDENTIALS_MASK_KEYS` 完整覆盖 4 个 longbridge 字段 + 3 个 encrypted_* 字段 + 通知渠道 URL。
- ✅ `AuditLogger.hash_actor()` 用 SHA256[:16]，永不存明文 key。
- ✅ WebSocket 鉴权用 `secrets.compare_digest()`，frame size 限 4096 字节防 DoS。
- ✅ SQL 注入：所有查询走 ORM parameter binding，无 f-string 拼 SQL。

## P2 / P3 候选（不修 — 与 YAGNI 一致）

| Severity | File | Line | 摘要 |
|----------|------|------|------|
| P3 | `backend/app/main.py` | 528 | 全局异常 handler 把 `type(exc).__name__` 返回给客户端，泄露 Python class 名 |
| P3 | `backend/app/api/credentials.py` | 79 | `logger.exception()` 可能记录包含字段名的 traceback |
| P3 | `backend/app/api/strategy.py` | 111 | `diff = {"detail": str(exc)}` 把原始 exception 存进 audit_logs |
| P2 | `backend/app/api/credentials.py` | 96 | masked 摘要 256 字符截断但仍带 str(exc)，可能泄露用户输入 |
| P2 | `backend/app/main.py` | 501 | CORS `cors_origins` 配置依赖 .env，需在 prod 部署检查 |

## 显式不做

- 端到端鉴权收紧（P2）：owner decision 2026-05-25 排除。
- 全局异常类型指纹消除（P3）：可信内网下不必要。

## Round 13 sentinel

不修代码，sentinel = 当前 948 passed 仍稳定。
