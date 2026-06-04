# oh-my-openagent 常驻团队部署报告

> **日期:** 2026-06-04
> **部署者:** Sisyphus(通过 executing-plans skill 实施 plan)
> **Plan:** `docs/superpowers/plans/2026-06-04-omo-teams-config-deployment.md`

## 部署完成度

| 阶段 | Task | 状态 | 备注 |
|---|---|---|---|
| 1 | 1. 基础环境确认 | ✅ | `~/.omo/teams/` 空(无冲突),Python 3.13.5 |
| 1 | 2-5. 4 个 config.json 写入 | ✅ | 全部 JSON 合法 + 16 subagent_type + 4 sisyphus lead |
| 1 | 6. 批量完整性验证 | ✅ | 4 目录 700,4 文件 600 |
| 1 | 7. doctor 验证 | ✅* | plugin 工作(11 agents + 8 categories 全识别),但 **doctor 4.5.12 不显式检查 `~/.omo/teams/`**;真正的 team 识别验证见 Phase 2 (Task 8) |
| 1 | 7.3. doctor log 归档 | ✅ | `docs/superpowers/deployment/2026-06-04-omo-teams-doctor-verbose.log` (109 行,未 add) |
| 2 | 8. 重启 opencode + `team_list` | ⏸ | **需用户操作**(Phase 2) |
| 2 | 9. 端到端 dry-run | ⏸ | 依赖 Task 8 |
| 2 | 10. 跨项目验证 | ⏸ | **需用户操作**(Phase 2) |

✅ = 已完成  ⏸ = 待用户操作(独立可执行 task 已全部完成)

## 部署位置

```
~/.omo/teams/                                  (drwx------)
├── code-review/                               (drwx------)
│   └── config.json                            (-rw-------, 1077 bytes)
├── security-audit/                            (drwx------)
│   └── config.json                            (-rw-------, 1197 bytes)
├── backend-resilience/                        (drwx------)
│   └── config.json                            (-rw-------, 1284 bytes)
└── frontend-quality/                          (drwx------)
    └── config.json                            (-rw-------, 1241 bytes)
```

## 4 个团队

| Team | Lead | Members (hephaestus / sisyphus-junior / atlas) |
|---|---|---|
| `code-review` | sisyphus | 深度/快速/跨模块三视角审视 |
| `security-audit` | sisyphus | 深度/快速/架构三视角安全审查 |
| `backend-resilience` | sisyphus | 深度/快速/跨服务三视角韧性审查 |
| `frontend-quality` | sisyphus | 深度/快速/跨视图三视角前端质量审查 |

## 关键配置

- **`~/.config/opencode/oh-my-openagent.json`**: `team_mode.enabled = true`, `max_parallel_members = 4`, `max_members = 8`, `tmux_visualization = false`(2026-06-03 已配,本次部署未改动)
- **Spec:** `docs/superpowers/specs/2026-06-04-omo-teams-design.md` (commit `c176c02`)
- **Plan:** `docs/superpowers/plans/2026-06-04-omo-teams-config-deployment.md` (677 行,**未 commit**)
- **Doctor log:** `docs/superpowers/deployment/2026-06-04-omo-teams-doctor-verbose.log` (未 add,可选 commit)

## 已知差异 vs Spec

1. **`bunx` → `npx`** — 用户环境无 `bunx`,用 `npx -y oh-my-opencode doctor` 等价执行;未来重跑 doctor 沿用 `npx`。
2. **Doctor 不显式检查 team configs** — `oh-my-opencode doctor 4.5.12` 报告内容(11 agents + 8 categories + 4 passed/2 warnings)中无 `team-mode` 段,不像文档描述的"includes a team-mode check"。可能原因:
   - 4.5.12 未实现该检查段(更新版有)
   - team-mode 检查需在 opencode TUI 进程内才触发(独立 doctor CLI 不查)
   - **真实验证路径**: 重启 opencode 后调用 `team_list` 工具(Task 8)
3. **Doctor 2 个 warnings 与本次部署无关**:
   - TUI plugin 缺失(`tui.json` 缺 `oh-my-openagent/tui` 条目)— pre-existing
   - gh CLI 缺失 — pre-existing
4. **prompt_append fallback** — 文档未明确列出 `prompt_append` 字段(agent schema 支持,TeamSpec 不明确);未实际触发 team_create,所以未触发回退。**真实情况需 Task 9 验证**。

## 回滚步骤

如需回滚到"team_mode 启用但无 teams"状态:

```bash
# 完全回滚(4 个 team 全删)
rm -rf ~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}
# team_mode.enabled 仍为 true,plugin 正常加载但无 team 可用

# 完全回到无任何 omo 团队状态(更彻底)
rm -rf ~/.omo/teams ~/.omo/.teams-backup
# 保留 ~/.omo/runtime/ 和 ~/.omo/worktrees/(其他可能用到)
```

**回滚后验证**:
```bash
ls ~/.omo/teams/ 2>&1  # 应显示 "No such file or directory" 或为空
```

## Phase 2 待用户操作清单

需要 opencode TUI 重启 + 端到端验证:

1. **Task 8** — 重启 opencode,在主会话中调用 `team_list`
   - 期望:返回 4 个 team(code-review, security-audit, backend-resilience, frontend-quality)
   - 不通过:检查 `~/.config/opencode/oh-my-openagent.json` 中 `team_mode.enabled` 仍为 `true`,确认 4 个 config.json 存在(已用 ls 验证)

2. **Task 9** — 端到端 dry-run
   - 调用 `team_create(name="code-review", task="...")` 测试一个 team 启动
   - 调 `team_status` 看 lead + 3 members
   - 调 `team_delete` 清理
   - 验证 `team_list` 仍返回 4(不被 delete 影响)

3. **Task 10** — 跨项目验证
   - 在 `/tmp/opencode-cross-project-test` 创建临时 git repo
   - 启动 opencode
   - 调 `team_list` 期望仍返回 4 teams(证明 user scope 跨项目复用)
   - 清理临时目录

## 引用

- Spec: `docs/superpowers/specs/2026-06-04-omo-teams-design.md`
- Plan: `docs/superpowers/plans/2026-06-04-omo-teams-config-deployment.md`
- Doctor log: `docs/superpowers/deployment/2026-06-04-omo-teams-doctor-verbose.log`
- oh-my-openagent team-mode 文档: <https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/docs/guide/team-mode.md>
