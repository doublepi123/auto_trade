# Review-Fix Loop 报告

- **目标**: P2 Strategy v2 影子策略全部未提交变更
- **时间**: 2026-07-12
- **审查焦点**: 正确性、因果性、持久化一致性、升级兼容、多标的调度与输入校验
- **总迭代次数**: 2
- **终止原因**: clean

## 迭代历史

### Iteration 1

**Review**: 发现 5 个问题

- [high] `backend/app/domain/strategy_v2/engine.py` — 待成交信号使用成交 K 线收盘后特征，形成下一根开盘成交的前视偏差。
- [high] `backend/app/services/strategy_v2_shadow_service.py` — 平仓后未立即 flush，可能在同一事务内读取到幽灵持仓。
- [high] `backend/app/services/strategy_v2_shadow_service.py` — 算法版本升级时，旧版本未平仓快照缺少明确的接管和退出路径。
- [high] `backend/app/main.py` — 定时任务只轮询主策略标的，其他已启用影子配置可能失去调度。
- [medium] `backend/app/services/strategy_v2_shadow_service.py` — 非法或非有限行情字段可能被静默跳过，掩盖数据源故障。

**Fix**: 修复 5 个问题

- `backend/app/domain/strategy_v2/engine.py` — 待成交仅验证会话、连续性、截止时间、次数和冷却，取消对成交 K 线收盘特征的依赖。
- `backend/app/services/strategy_v2_shadow_service.py` — 平仓写入后立即 flush，确保同事务查询一致。
- `backend/app/services/strategy_v2_shadow_service.py` — 旧版本持仓继续按冻结版本管理至退出，随后前向切换到当前版本；旧版本空仓则首个观测点直接重置。
- `backend/app/main.py` — 调度主标的、所有已启用配置和仍有虚拟持仓的标的，并隔离单标的失败。
- `backend/app/services/strategy_v2_shadow_service.py` — 严格拒绝 NaN、非有限值、非法 OHLC 和负成交量，并把无可处理数据记录为轮询错误。

### Iteration 2

**Review**: 未发现需要修改的问题。

**Fix**: 无。

## 全量回归补充

在 review-fix loop clean 后的跨栈回归中又发现并关闭 4 组问题：

- [high] `frontend/src/views/Strategy.vue` — 多个 P0 安全字段的 `min/step` 与默认值或后端小数精度不相容，浏览器原生校验会静默阻断所有策略保存请求。
- [medium] `frontend/cypress/e2e/trade_roundtrips.cy.ts`、`frontend/cypress/support/e2e.ts` — TradeStats fixture 缺少当前 API 必需字段，费用来源文案断言仍停留在旧契约。
- [medium] `frontend/cypress/e2e/strategy_status_refresh.cy.ts` — 局部策略 fixture 缺少成本字段，可能生成 `NaN -> null` patch 并造成测试假绿。
- [medium] `frontend/cypress.config.ts` — Cypress 15 的兼容开关允许浏览器代码读取全部测试环境变量；仓库无相关调用，已显式关闭。

修复后定向复核 clean；前端全量 71 个 spec、257 项测试全部通过。

## 最终状态

- 所有发现的问题: 9
- 已修复: 9
- 无法修复: 0
- 循环检测触发: 否
- 初始内容哈希: 未捕获（loop 在实现完成后启动，未伪造基线）
- 最终目标文件内容 SHA256: `eb032625dc188cd1d3aef55e6dc232e6d1b4f16a43656bef8758f418107cda22`
- 独立复核: clean；相关测试 51 项通过
- P2 聚焦测试: 96 项通过
- 后端全量: 4147 passed, 1 skipped；覆盖率 87.48%
- BasedPyright: 0 errors / 0 warnings / 0 notes
- 前端: type-check、生产 build、Cypress 257/257 通过
- 容器验收: 双容器 healthy；鉴权 401、影子启用、180 决策 replay、数据库零写入断言和桌面/移动端真实页面检查通过

## 最终建议

保持 P2 为纯影子模式，至少积累 20 个交易日和 50 笔已平仓虚拟交易，再依据净收益、命中率、最大不利变动、时段和市场状态分层结果评估是否进入 P3/P4。
