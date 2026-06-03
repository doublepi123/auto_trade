# oh-my-openagent 通用常驻团队 设计

> **日期：** 2026-06-04
> **代号：** OMO-TEAMS（oh-my-openagent Team Mode 常驻团队库）
> **基线：** `oh-my-openagent.json` 已开启 `team_mode`（`enabled: true, max_parallel_members: 4, max_members: 8, tmux_visualization: false`，2026-06-03 配置），`pytest 730 passed` / `basedpyright 0 errors` / `vue-tsc` clean。
> **目标文件：** `~/.omo/teams/{name}/config.json`（user scope，跨项目复用）
> **前置阅读：**
> - `oh-my-openagent` 官方 [`docs/guide/team-mode.md`](https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/docs/guide/team-mode.md)
> - 本地 `~/.config/opencode/oh-my-openagent.json` 中 `team_mode` 块

---

## 1. 背景与动机

`team_mode` 已在 user scope 开启，但 `~/.omo/teams/` 仍为空，团队需要按需创建。为最大化 Team Mode 的长期价值：

- **跨项目复用：** 项目级团队配置会随仓库迁移而漂移，user scope 团队在所有项目自动可用，与 `~/.config/opencode/oh-my-openagent.json` 一致定位。
- **常驻能力 vs 任务委派：** 当前 P24 迭代（技术债清扫 + Toast）的 7 个 plan 是**单次任务**；常驻团队是**能力单元**，可被任意后续任务复用，回报周期更长。
- **避免反复定义：** 团队 config 一旦写好，跨项目零成本；每次新需求时不用重新设计 member 构成。

本设计沉淀 4 个常驻团队，覆盖 `auto_trade` 项目全栈（FastAPI 后端 + Vue 前端）的核心质量维度，可立即用于 P24 7 个 plan，也可用于未来所有迭代。

---

## 2. 关键设计决策

| # | 决策 | 选项 | 选择 | 理由 |
|---|------|------|------|------|
| D1 | 团队数量与定位 | 混合 / 常驻通用 / P24 专用 / 极简 | **常驻通用** | user scope 跨项目复用，不绑定具体迭代 |
| D2 | 能力覆盖 | 3 个核心 / 4 个平衡 / 5 个全覆盖 / 文档实验导向 | **4 个平衡** | fullstack 项目需前后端都覆盖 + 经典常驻价值（审查+安全） |
| D3 | Member 风格 | 专业化 category / 通用 subagent / 混合 / 全 category | **通用 subagent 直挂** | 设置成本低，跨项目一致行为；`prompt_append` 提供角色引导 |
| D4 | Lead agent | sisyphus / atlas / sisyphus-junior | **sisyphus** | 文档列为"eligible"且调度能力最强 |
| D5 | Member 数量 | 1 / 2 / 3 / 4 | **3** | 覆盖多视角且低于 `max_parallel_members=4` 留 lead 位 |
| D6 | worktree 隔离 | 开启 / 关闭 | **关闭** | 单分支开发习惯 + git 复杂度成本高于收益；项目级 git 操作（rebase/merge）需在主 worktree 执行 |
| D7 | Scope | user / project | **user** | 跨项目复用；与 `oh-my-openagent.json` 同级 |

**取消考虑的方案：**
- **每个 team 加 `enabled` 开关**（文档不支持顶层 switch，仅有 `team_mode.enabled`）→ 取消
- **per-team 强制声明 `disabled_skills`**（与 `team_mode.disabled_skills` 顶层字段重叠）→ 取消
- **per-team `prompt`（完整 system prompt）**（与 D3 直挂风格冲突）→ 取消，改为短 `prompt_append`

---

## 3. 4 个团队清单

| Team | 关注领域 | 典型触发场景 |
|---|---|---|
| `code-review` | 命名、重复、架构、类型安全、可测试性 | PR review / 合并前自检 / 任何改动后的快速质量门 |
| `security-audit` | OWASP Top、密钥、auth 链路、依赖 CVE | 改动含 auth/crypto/network / 定期扫描 / 上线前 |
| `backend-resilience` | 重试/退避、对账、锁、错误处理、断连恢复 | runner/broker/risk 模块改动 / 加新外部依赖 / 重构 |
| `frontend-quality` | ai-slop、组件拆分、props/composables、TS 严格性、可访问性 | view/composable 改动 / 前端重构 / 加新页 |

**三视角分工模式**（所有团队统一）：
- `hephaestus` — **深度视角**：架构、复杂逻辑、重构机会
- `sisyphus-junior` — **快速视角**：异味、命名、testid、TS 严格性、模板代码
- `atlas` — **跨模块视角**：跨文件/服务影响、契约、状态管理、覆盖缺口

---

## 4. 完整 config.json 草稿

### 4.1 `~/.omo/teams/code-review/config.json`

```json
{
  "name": "code-review",
  "description": "Code quality review across naming, duplication, architecture, type safety, and testability.",
  "lead": {
    "kind": "subagent_type",
    "subagent_type": "sisyphus"
  },
  "members": [
    {
      "kind": "subagent_type",
      "subagent_type": "hephaestus",
      "prompt_append": "你是 code-review 团队的深度审查员。关注架构合理性、复杂逻辑可读性、重构机会。给出可执行的具体修改建议,而非泛泛而谈。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "sisyphus-junior",
      "prompt_append": "你是 code-review 团队的快速扫描员。关注命名一致性、重复代码、缺失 testid、不必要的样板代码、类型定义冗余。用列表快速给出问题清单。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "atlas",
      "prompt_append": "你是 code-review 团队的跨模块审视员。关注跨文件/跨服务影响: API 契约变化、状态管理一致性、测试覆盖缺口、文档同步需求。"
    }
  ]
}
```

### 4.2 `~/.omo/teams/security-audit/config.json`

```json
{
  "name": "security-audit",
  "description": "Security audit covering OWASP top issues, secrets, auth chains, dependency risks.",
  "lead": {
    "kind": "subagent_type",
    "subagent_type": "sisyphus"
  },
  "members": [
    {
      "kind": "subagent_type",
      "subagent_type": "hephaestus",
      "prompt_append": "你是 security-audit 团队的深度安全审查员。关注 SQL 注入、路径穿越、反序列化、SSRF、命令注入、认证/会话漏洞、不安全随机数。分析数据流,标注可利用性。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "sisyphus-junior",
      "prompt_append": "你是 security-audit 团队的快速扫描员。关注:硬编码密钥/凭证、缺失的输入校验、危险函数使用(eval/exec/pickle/subprocess shell=True)、明文日志敏感信息、依赖 CVE 风险。grep 全仓,列具体文件:行号。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "atlas",
      "prompt_append": "你是 security-audit 团队的架构安全审视员。关注鉴权链路、中间件顺序、CORS/CSRF、rate limit、密钥管理(RSA/AES-GCM)、审计日志完整性、数据最小化。"
    }
  ]
}
```

### 4.3 `~/.omo/teams/backend-resilience/config.json`

```json
{
  "name": "backend-resilience",
  "description": "Backend resilience review: retry/backoff, reconciliation, locking, error handling, recovery from disconnect.",
  "lead": {
    "kind": "subagent_type",
    "subagent_type": "sisyphus"
  },
  "members": [
    {
      "kind": "subagent_type",
      "subagent_type": "hephaestus",
      "prompt_append": "你是 backend-resilience 团队的深度韧性审查员。关注:重试/退避是否合理(指数退避?jitter?)、对账机制、锁使用正确性、transaction 边界、信号处理(SIGTERM/SIGINT)下的资源清理、关键路径幂等性。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "sisyphus-junior",
      "prompt_append": "你是 backend-resilience 团队的快速异常审计员。grep `try:` / `except:` / `catch(e` / `pass` 等模式,关注:空 except 块、异常吞噬、错误吞下、错误信息泄露内部状态、finally 块缺失。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "atlas",
      "prompt_append": "你是 backend-resilience 团队的跨服务链路审视员。关注:runner↔broker↔DB↔WebSocket 链路上的超时设置、circuit breaker、消息可靠性保证、状态机在异常下的可恢复性、SDK 断开重连覆盖。"
    }
  ]
}
```

### 4.4 `~/.omo/teams/frontend-quality/config.json`

```json
{
  "name": "frontend-quality",
  "description": "Frontend quality: AI slop removal, component design, props/composables, TypeScript strictness, accessibility.",
  "lead": {
    "kind": "subagent_type",
    "subagent_type": "sisyphus"
  },
  "members": [
    {
      "kind": "subagent_type",
      "subagent_type": "hephaestus",
      "prompt_append": "你是 frontend-quality 团队的深度组件审查员。关注:组件拆分是否合理(单一职责)、props 设计(过多?默认?类型?)、composables 复用、响应式数据流(避免不必要 ref/computed)、Vue 3.5 最佳实践。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "sisyphus-junior",
      "prompt_append": "你是 frontend-quality 团队的快速异味扫描员。关注:重复代码、缺失 testid(对 cypress 至关重要)、魔术串(应集中到 labels.ts)、TS any/类型断言、未使用的 import、过长行。"
    },
    {
      "kind": "subagent_type",
      "subagent_type": "atlas",
      "prompt_append": "你是 frontend-quality 团队的跨视图审视员。关注:路由守卫、状态管理一致性、API 客户端复用、WebSocket 集成、错误处理/用户反馈(ElMessage)覆盖、加载/空/错误态。"
    }
  ]
}
```

> **关于 `version` / `createdAt` / `leadAgentId`：** 文档声明由 loader 自动填充，无需手写。
>
> **关于 `prompt_append` vs `prompt`：** 选用 `prompt_append` 是基于 agent schema 文档（顶层 agents 块明确支持 `prompt_append`），TeamSpec 文档未明确列出此字段。**风险：** 如果 `team_create` 启动时报 `unknown field`，回退方案是把 `prompt_append` 改为 `prompt`（整段覆写默认 system prompt）。该回退不影响 lead/member 关系，仅丢失 agent 自身的默认行为。这是设计已接受的退化。

---

## 5. 文件结构

```
~/.omo/
├── runtime/           # 团队运行时状态（已存在）
├── teams/             # 已声明的团队规格（本设计新增）
│   ├── code-review/config.json
│   ├── security-audit/config.json
│   ├── backend-resilience/config.json
│   └── frontend-quality/config.json
└── worktrees/         # 可选 per-member worktree（本设计不启用）
```

`~/.omo/teams/` 目录权限继承现有 `700`（drwx------）。

---

## 6. 启用流程

### 6.1 写入阶段

1. 备份当前 `~/.config/opencode/oh-my-openagent.json`（已完成，2026-06-03T18-43 备份存在）
2. 创建 4 个目录：`mkdir -p ~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}`
3. 写入 4 个 config.json（按 §4 内容）
4. 每个文件用 `python3 -c "import json; json.load(open(p))"` 验证 JSON 合法
5. （可选）`chmod 600` 各 config.json（团队规格含 role 描述，不算敏感但建议收紧）

### 6.2 验证阶段

```bash
# 1. JSON 合法性
python3 -c "import json; [json.load(open(f'~/.omo/teams/{t}/config.json')) for t in ['code-review','security-audit','backend-resilience','frontend-quality']]; print('OK: 4 files valid')"

# 2. Doctor 报告
bunx oh-my-opencode doctor
# 应在 team-mode 检查项下看到 4 declared teams

# 3. 实际跑一次（dry-run）
# 重启 opencode 后在 Sisyphus 主会话执行：
#   team_create(name="code-review", task="Review README.md")
#   team_status
#   team_delete
```

### 6.3 启用判定

- `doctor` 输出 "4 declared teams" → 启用成功
- `team_list` 列出 4 个 team → 启用成功
- 任何失败 → 删除对应文件即可（user scope 写入是纯增量操作，无回滚需求；`team_mode.enabled = true` 本身无任何副作用，无团队时仅 `team_list` 返回空数组）

---

## 7. 使用模式

### 7.1 标准使用流程（以 code-review 为例）

1. 用户在 Sisyphus 主会话发起："用 code-review 团队审查最近 3 个 commits"
2. Sisyphus 调 `team_create(name="code-review", task="...", context="git log -3 --stat")`
3. Lead sisyphus 解析任务，创建 task list
4. Lead 通过 `team_send_message` 委派给 3 个 member（hephaestus / sisyphus-junior / atlas）
5. 3 个 member 平行工作，通过 mailbox 通信
6. 完成后 lead 写汇总 → `team_shutdown_request` → 各 member 关闭 → `team_delete` 清理

### 7.2 组合使用

- **完整 PR review**：`code-review` + `security-audit` + `backend-resilience`（后端改动）/`frontend-quality`（前端改动）
- **上线前自检**：`security-audit` + `backend-resilience`（如有后端改动）
- **重构验证**：`code-review`（重构后）→ `backend-resilience`（涉及后端）→ `frontend-quality`（涉及前端）

### 7.3 边界

- **成员间无嵌套团队**（文档硬限制）→ 不能在 team 内部再 `team_create`
- **同步阻塞不允许**（`team_send_message` 是 fire-and-forget）→ 不期望立即回复
- **lead-only broadcast** → 普通成员不能广播，只能点对点

---

## 8. 验证与测试

| 项目 | 命令 | 通过标准 |
|---|---|---|
| JSON 合法性 | `python3 -c "import json; json.load(open(p))"` × 4 | 无异常 |
| 目录权限 | `ls -la ~/.omo/teams/` | `drwx------` |
| Doctor 报告 | `bunx oh-my-opencode doctor` | team-mode 段显示 4 teams |
| 工具可用性 | 重启 opencode 后主会话试 `team_list` | 返回 4 个 team |
| 端到端 | `team_create` + `team_status` + `team_delete` 跑通 | 完整生命周期无错 |
| 跨项目 | 在另一项目（`/tmp/opencode-test`）启动 opencode | `team_list` 同样返回 4 个 team |

---

## 9. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| Member prompt_append 中文长串在某些 agent 下被截断 | 低 | 已控制在 100 字内，文档无明确长度限制 |
| 3 个 member 同时跑导致 token 费用激增 | 中 | 仅在用户显式调用时启动；空 `~/.omo/teams/` 不消耗任何资源 |
| 团队规格错误导致 `team_create` 失败 | 低 | `doctor` 命令前置检查；失败时 lead 应给出明确错误 |
| `atlas` / `sisyphus-junior` 等 agent 在不同 oh-my-openagent 版本行为不一致 | 低 | 4 个 agent 都是 oh-my-openagent 内置 official agents，跨小版本稳定 |
| worktree 关闭导致成员改冲突 | 中 | 文档硬性要求所有 member 写在同一 worktree，团队仅做"审视"不直接 commit |
| Lead 调度卡死 | 低 | `max_wall_clock_minutes=120`（默认）兜底，文档保证硬停 |

---

## 10. YAGNI 显式切除

- ❌ worktree 隔离（默认关闭）
- ❌ 动态团队模板生成（用户/项目级 jinja 模板）
- ❌ per-team `enabled` 开关（与 `team_mode.enabled` 重叠，文档不支持）
- ❌ per-team 持久化 runtime 配置（用 `~/.omo/runtime/` 默认行为）
- ❌ 团队统计/使用率 dashboard（`team_status` 足够）
- ❌ 自动触发（hooks）—— Team Mode 文档未提供；用户显式调用
- ❌ tmux 可视化（环境未装 tmux，`tmux_visualization: false` 已在 oh-my-openagent.json 中设置）
- ❌ 项目级 `.omo/teams/` 覆盖（user scope 跨项目统一）
- ❌ 修改 `team_mode` 顶层其他字段（用默认：`max_messages_per_run=10000`, `max_wall_clock_minutes=120`, `max_member_turns=500`, `base_dir=~/.omo`, `message_payload_max_bytes=32768`, `recipient_unread_max_bytes=262144`, `mailbox_poll_interval_ms=3000`）

---

## 11. 后续扩展方向（不在本设计范围）

- **LLM prompt 实验团队**（与 `docs/superpowers/specs/2026-05-28-llm-prompt-engineering-optimization-design.md` 配合）：当 prompt 版本管理需求成熟时，加 `prompt-experiment` team
- **回测分析团队**：与 P7' 策略复盘设计配合
- **迁移审计团队**：用于大规模跨文件/跨服务迁移
- **per-team `disabled_skills`**：这是**新增**配置项（与 §10 YAGNI 取消的 `enabled` 开关不同），如果某些团队需要禁用特定 skill（如 `disabled_skills: ["playwright"]`）时
- **per-team 模型覆盖**：当某团队特定角色需要 `category` 风格时（D3 决策可在后续需要时反转）
- **`prompt_append` 验证后如不支持**：统一迁移到 `prompt` 字段

---

## 12. 参考资料

- `oh-my-openagent` 官方文档：[`docs/guide/team-mode.md`](https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/docs/guide/team-mode.md)
- `oh-my-openagent` 官方 README：[`README.md`](https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/README.md) — Team Mode 章节
- 配置 schema：[`oh-my-opencode.schema.json`](https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/assets/oh-my-opencode.schema.json)
- 本地 `~/.config/opencode/oh-my-openagent.json`（`team_mode` 块已配置，2026-06-03）
- `docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md`（P24 7 plan 是常驻团队的首批目标）

---

## 13. 实施检查清单

- [ ] 创建 4 个目录 `~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}`
- [ ] 写入 4 个 config.json
- [ ] 4 个文件 JSON 合法性验证
- [ ] `bunx oh-my-opencode doctor` 显示 4 teams
- [ ] 重启 opencode，`team_list` 列出 4 个 team
- [ ] `team_create` + `team_status` + `team_delete` 端到端跑通
- [ ] 跨项目验证（任意其他目录启动 opencode，`team_list` 同样返回 4 个）
