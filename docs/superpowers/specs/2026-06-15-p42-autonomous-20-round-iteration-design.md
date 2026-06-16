# P42：20 轮自主迭代设计

> 用户指令："制定后续迭代计划，并进行迭代" → 方向："自主决定 + 子 agent 并行"（类比 P20 的 20 轮自主迭代，但起点是 P41 之后的更成熟状态：pytest 903 passed / 覆盖率 87% / basedpyright 0/0/0 / vue-tsc 0 / Cypress 80 项）。
>
> 本 spec 不再为每一轮固定主题——主题由首轮 subagent review 扫描与每轮反馈决定。本 spec 锁定的只是 **20 轮自主迭代的纪律、约束与产出形式**。

## 背景

- 最近一次 review-fix loop 是 `4c7961f fix: 14-round review-fix loop — ~170 fixes, 0 regressions`（2026-06-10 左右），已过去近 5 天；这段时间累积了：
  - P41 交易报告 + 增强复盘（2026-06-07 交付）
  - P32~P40 多标的架构（已分 4+ wave 落地）
  - P35~P36 LLM cron 预算 + Element Plus 按需
  - P37~P39 多标的状态 composables、symbol debug export、全局控制审计
- Roadmap 候选：P41 后是 P42（构建告警清扫）/P43（导出图表联动）/P44（审计与导出联动）/P45（运维时间线统一视图）。这 4 项已知的 P 系列价值是"运维可观测性增强链"，但每项都是中等工时、价值递增。
- 项目最近 5 天没有 review-fix 循环，技术债审计频次偏低。

## 目标

**自主决定 20 轮迭代方向**，每轮聚焦"修复 / 增强 / 收尾"三类中的一种，单轮可独立 ship、验证、回滚。**不**要求每轮都新增 P-series 编号；多数轮次只是 review-fix 续篇。

## 范围

### 20 轮结构

| 轮 | 类型 | 描述 |
|----|------|------|
| 1 | **Review 扫描** | fan-out 5–6 个 subagent 沿维度（后端 correctness / 后端韧性 / 前端 UX / 测试覆盖 / 安全 / 性能）做全项目 review，汇总候选清单并去重，按价值/风险排序。 |
| 2 | **数据真实性 + 死代码** | 处理 1 轮扫描里任何"数据被合成 / 字段长期 unused / pyflakes 命中"等明确 P0/P1 项。 |
| 3 | **前端一致性** | 处理前端 API 类型契约、a11y、loading/error 状态不一致等。 |
| 4 | **后端韧性** | broker 错误路径、pending reconcile、API error envelope 的一致性。 |
| 5 | **测试加固** | 收尾 flaky / time-zone / DST 边界 / 并发死锁等已知 test issue。 |
| 6 | **Review 扫描 2** | 跑第 2 次 review（不同角度：观察列表 + 报告 + Lab + 实验），与第 1 轮结果去重。 |
| 7–14 | **P-series 候选实施** | 依扫描结果与 Roadmap 候选，按价值/风险选 2–4 个 P-series 子迭代（如 P42 构建告警清扫 → P43 导出图表联动 → P44 审计与导出联动 → P45 运维时间线）。每轮可 1 个子迭代。 |
| 15 | **P-series 收尾** | 处理 P43/P44/P45 中遗留的 UI polish / 文档 / Cypress 增补。 |
| 16 | **死代码 / 文档** | vulture + pyflakes + 未引用 css 类、未使用 export、CSS 死代码、CHANGELOG 同步。 |
| 17 | **Review 扫描 3** | 第 3 次 review（不同维度：安全 / 性能 / 类型契约 / 测试盲区）。 |
| 18–19 | **杂项收尾** | 处理 1+2 轮 review 累积的 P3 候选。 |
| 20 | **总结** | 写本轮交付摘要到 Roadmap，列下批 P46+ 候选。 |

### 优先级原则

- **数据真实性 > 死代码 > 一致性 > 收尾**。修复"伪造 LLM 数据"或"长期 unused 函数"比加新功能价值更高。
- **不引入跨轮 ping-pong 改动**。一旦某轮定型，下一轮不再回滚；若发现冲突，留在下批处理。
- **每轮不超过 1 个独立子主题**。20 轮 = 20 个可独立 ship 的 commit 组。

### 子 agent 编排

- **Review 子 agent**：只读，限定在指定子目录，返回结构化 finding（file_path / line / severity / fix sketch）。
- **实施子 agent**：限定文件范围（用 `pathspec` 严格匹配），写代码 + 测试，最后跑该子模块 pytest + basedpyright。
- **验证子 agent**：在每轮结束后跑对应 pytest 文件 + basedpyright + Cypress spec，确认不引入回归。
- **Orchestrator（本主 agent）**：调度 + 整合 + 决定次序；不直接写实现。

### 验收口径

每轮交付后必须满足：

- `pytest tests/ -v` 全绿（基线 903 passed，新增失败视为回归）。
- `basedpyright` 0 errors / 0 warnings / 0 notes。
- `npm run type-check` 0 errors。
- `npm run build` 通过（chunk 预算不退步）。
- 涉及前端的轮次：新增 / 改动的 Cypress spec 全绿。
- 涉及数据库的轮次：旧库 `_ensure_*` 兼容迁移存在。

### 显式不做（与已有 YAGNI 决策保持一致）

- 节假日历
- 审计 CSV/JSON 导出增强（已部分在 P39 完成）
- Webhook 模板编辑器
- 通知重发队列 UI
- 高频交易 / 复杂择时指标
- 量化研究平台
- API 鉴权收紧（owner decision 2026-05-25）

## 非目标

- 不在本轮重启 P2（API 鉴权）。
- 不开 P46+（留作下批 20 轮）。
- 不在主分支之外 push（worktree 隔离；用户未要求 commit 时不 commit）。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Subagent 越权改其他文件 | `pathspec` 锁定；实施 agent 给定"允许改"白名单。 |
| 跨轮 ping-pong | 引入 loop 计数器，相同 file_path 出现 ≥3 次则停下来人工审视。 |
| pytest 跑不起来（Python 3.13 vs 项目 3.11+） | 本轮用 `.venv` 或新建 3.11 venv；若不可用，提交 subagent 仅做静态分析 + 单文件 import smoke。 |
| Cypress 跑不起来（需 Vite dev server） | subagent 只做类型 + 构建检查；Cypress 在主 agent 控制下做。 |
| Subagent 返回非结构化文本 | 用 `schema` 参数强约束 JSON schema。 |

## 验收命令

```bash
# 每轮末必须全绿
cd backend && python3 -m pytest tests/<touched> -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
```

20 轮全部结束后：

```bash
cd backend && python3 -m pytest tests/ -v      # 期望 920+ passed
cd backend && python3 -m basedpyright            # 0/0/0
cd frontend && npm run type-check                # 0 errors
cd frontend && npm run build                     # pass + chunk budget hold
cd frontend && npm run cypress:run -- --spec cypress/e2e/<touched>.cy.ts
```

## 备注

- 本 spec 实际是 P20 的迭代结构（20 轮自由迭代 + 多 subagent）以更新基线。P20 的基线是 692 passed；本轮基线是 903 passed。
- 本轮不重新编号 Roadmap 表中的 P42–P45。若轮次中实施的 P-series 编号与 Roadmap 表对齐，会同步更新该表。
- 用户指令"自主决定 + 子 agent 并行"等价于 P20 模式，但 P20 是 2026-05 月，本轮是 2026-06 月 15 日，所以本批为 P42。
