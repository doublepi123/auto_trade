# E. 4 个活跃分支 rebase + 合并 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 4 个落后于 main 的本地分支有序合入 main，消除后续迭代 rebase 冲突风险。

**Architecture:** 按 ahead 数量降序合并（addon-buy-margin → dashboard-config-perf → p5-plus-audit → maintainability-frontend）。每分支：rebase + squash + 合并 + 全测验证。冲突点手工解。

**Tech Stack:** git / pytest 9 / basedpyright / vue-tsc / Cypress

**前置阅读：**
- 母 spec §4.5
- git status 当前工作树必须干净

---

## 前置条件（必须满足）

- [ ] 工作树干净（`git status` 无未提交修改）
- [ ] 4 个分支均存在：
  - `feature/addon-buy-margin-sizing`（ahead 7，behind 14）
  - `feature/dashboard-config-performance`（ahead 1，behind 139）
  - `p5-plus-audit-notifications`（ahead 1，behind 89）
  - `refactor/maintainability-frontend`（ahead 1，behind 202）
- [ ] Wave 1 + Wave 2 已合入 main 且全绿
- [ ] `origin/main` 与 `main` 同步

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Create | `.sisyphus/evidence/task-E-branch-merge-log.md` | 合并日志：每分支 squash commit hash + 冲突解点 |
| Merge | main 4 次（每个分支 1 次） | 顺序合并 |

> **不创建新代码文件**。

---

## 任务 1: 主分支冻结 + 准备

### Step 1.1: 确认工作树干净

```bash
cd /home/lcy/code/auto_trade
git status
git log --oneline -5
```

预期：clean working tree。

- [ ] **Step 1.1**: 工作树干净

### Step 1.2: 切换到 main + pull

```bash
git checkout main
git pull origin main
```

预期：与 origin/main 同步。

- [ ] **Step 1.2**: main 同步

### Step 1.3: 写合并日志骨架

```bash
cat > .sisyphus/evidence/task-E-branch-merge-log.md << 'EOF'
# 4 分支合并日志 (Task E)
# 开始时间: <YYYY-MM-DD>
# 合并顺序: addon-buy-margin → dashboard-config-perf → p5-plus-audit → maintainability-frontend

## 1. feature/addon-buy-margin-sizing
- 原始 ahead: 7, behind: 14
- squash commit: <hash>
- 冲突点: <description>
- 验证: pytest / basedpyright / vue-tsc / build / cypress

## 2. feature/dashboard-config-performance
...

## 3. p5-plus-audit-notifications
...

## 4. refactor/maintainability-frontend
...
EOF
```

- [ ] **Step 1.3**: 写日志骨架

---

## 任务 2: 合并 feature/addon-buy-margin-sizing（P14 margin safety factor）

### Step 2.1: 切到分支

```bash
git checkout feature/addon-buy-margin-sizing
```

- [ ] **Step 2.1**: 切分支

### Step 2.2: 跑全测（合并前基线）

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
```

预期：本分支独立通过（分支本来就过测试）。

- [ ] **Step 2.2**: 跑全测

### Step 2.3: rebase main

```bash
git rebase main
```

预期：可能冲突（因 main 已演进 14 commit）。冲突点：

- `backend/app/core/broker.py`（margin safety factor 相关）
- `frontend/src/views/Strategy.vue`（margin safety 字段）

> 实际冲突点取决于 main 演进；按 git 提示处理。

- [ ] **Step 2.3**: rebase（处理冲突）

### Step 2.4: rebase 后跑全测

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 2.4**: 验证

### Step 2.5: squash 为单 commit

```bash
# 查看 commit 数
git log --oneline main..HEAD | wc -l
# 交互式 rebase squash
git rebase -i HEAD~$(git log --oneline main..HEAD | wc -l)
# 在编辑器中：除第一个 commit 保留 `pick` 外，其他全部改为 `squash` 或 `fixup`
# 保存退出
```

预期：单 commit（保留主题 commit message：`feat(strategy): add margin safety factor field to strategy form` 或类似）。

- [ ] **Step 2.5**: squash

### Step 2.6: 合并到 main

```bash
git checkout main
git merge --no-ff feature/addon-buy-margin-sizing -m "Merge branch 'feature/addon-buy-margin-sizing' (squashed)"
```

预期：fast-forward 或 merge commit。

- [ ] **Step 2.6**: 合并

### Step 2.7: 跑全栈质量门禁

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 2.7**: 验证

### Step 2.8: 记录到合并日志

```bash
# 编辑 .sisyphus/evidence/task-E-branch-merge-log.md
# 填入：squash commit hash、冲突解点、验证结果
```

- [ ] **Step 2.8**: 记录

---

## 任务 3: 合并 feature/dashboard-config-performance（P15 Dashboard 性能）

### Step 3.1~3.8: 同任务 2 流程

针对 `feature/dashboard-config-performance`（ahead 1）：

- 切分支
- 跑全测
- rebase main（可能冲突：因 main 已有 P15 提交 → 几乎 0 冲突，fast-forward 即可）
- squash（仅 1 commit，无需 squash）
- 合并
- 验证
- 记录

- [ ] **Step 3.1~3.8**: 合并

---

## 任务 4: 合并 p5-plus-audit-notifications（P5+ 通知）

### Step 4.1~4.8: 同任务 2 流程

针对 `p5-plus-audit-notifications`（ahead 1）：

**特别注意：** P5+ 审计 + 多渠道通知已在 main 完成（Roadmap 546 行）。本分支可能与 main 重叠。

- 切分支后看 diff：`git diff main...HEAD --stat`
- 若功能 100% 重叠 → 跳过此分支（不合并），记录"已包含在 main"
- 若有独有改进 → rebase + squash + 合并

- [ ] **Step 4.1~4.8**: 合并（或跳过）

---

## 任务 5: 合并 refactor/maintainability-frontend

### Step 5.1~5.8: 同任务 2 流程

针对 `refactor/maintainability-frontend`（ahead 1）：

**特别注意：** maintainability refactor 大部分已在 main（P5 2026-05-17 commit 76-82 行）。本分支可能与 main 大量重叠。

- 切分支后看 diff：`git diff main...HEAD --stat`
- 若 diff 极小（<50 行）→ 直接合并（fast-forward 即可）
- 若 diff 大 → 评估是否需要，必要时跳过并记录

- [ ] **Step 5.1~5.8**: 合并（或评估后跳过）

---

## 任务 6: 最终验证 + Commit

### Step 6.1: 跑全栈 + Cypress

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 6.1**: 全栈验证

### Step 6.2: 检查 main 历史

```bash
git log --oneline -10
```

预期：4 个 merge commit（或 fast-forward 后 4 个新 commit 紧接 main）。

- [ ] **Step 6.2**: 查历史

### Step 6.3: 删除已合并分支（可选）

```bash
git branch -d feature/addon-buy-margin-sizing
git branch -d feature/dashboard-config-performance
git branch -d p5-plus-audit-notifications
git branch -d refactor/maintainability-frontend
```

> 注意 `-d`（小写）只在已合并时删；如有问题用 `-D` 但需确认已合并。

- [ ] **Step 6.3**: 删分支

### Step 6.4: 写证据完成

```bash
# 编辑 .sisyphus/evidence/task-E-branch-merge-log.md
# 填入：4 分支合并结果、删除的分支、main 当前 commit
```

- [ ] **Step 6.4**: 写证据

### Step 6.5: Commit（证据文件 + 分支删除）

```bash
git add .sisyphus/evidence/task-E-branch-merge-log.md
git commit -m "docs: record 4-branch merge log for task E"
```

> ⚠️ **不主动 commit**。agent 输出 "Task E complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 4 分支按顺序合入 main（或评估后跳过）
- [ ] main 工作树干净
- [ ] 全栈 pytest + basedpyright + vue-tsc + build + cypress 全绿
- [ ] 合并日志 `.sisyphus/evidence/task-E-branch-merge-log.md` 已写完整
- [ ] 已合并分支已删除（可选）
- [ ] **不主动 commit**

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| 4 分支内容已在 main（重复） | 评估 diff：< 50 行直接合并；> 50 行评估后跳过 |
| rebase 冲突面积大 | 冲突点手工解；不解时 `git rebase --abort` 并升级到用户 |
| 合并后回归测试挂 | 立即 `git revert <merge-commit>` 回滚，记录原因 |
| 误删未合并分支 | 仅用 `git branch -d`（小写，已合并才能删）；如 `-D` 必须确认 |
| E 期间 main 又有新合入 | 显式冻结：通知所有 agent 本期间 main 仅 E 操作 |

## 范围外（YAGNI）

- ❌ 修改分支内容（仅合并）
- ❌ 解冲突时引入新功能（仅保持原分支内容）
- ❌ 强制 fast-forward（用 `--no-ff` 保留分支拓扑）
- ❌ 推送到 origin（用户显式指令时再推）

---

**Plan E 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.5。**
