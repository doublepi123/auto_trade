# oh-my-openagent 常驻团队配置文件部署 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `~/.omo/teams/` 部署 4 个常驻 team 配置文件,完成 JSON 合法性、doctor 报告、端到端 dry-run 和跨项目复用四项验证,达到"打开 oh-my-openagent TUI 即可用 4 个 team"的完成态。

**Architecture:** User scope 配置 — 4 个独立 `config.json` 各自封装一个 team 规格;每个 team 由 1 个 sisyphus lead + 3 个 subagent 直挂 members 组成;`prompt_append` 提供角色引导;无 git 操作(`~/.omo/teams/` 不在项目仓库)。

**Tech Stack:**
- oh-my-openagent v4.0+ (Team Mode 已通过 `oh-my-openagent.json` 启用,2026-06-03)
- Python 3 (JSON 合法性验证)
- `bunx oh-my-opencode` (doctor 报告)
- OpenCode TUI (`team_*` 工具)
- Spec 源:`docs/superpowers/specs/2026-06-04-omo-teams-design.md` (commit `c176c02`)

---

## File Structure

本 plan **仅在 `~/.omo/teams/` 下创建 4 个 config.json**,**不修改任何项目文件**(不修改 `backend/`, `frontend/`, `docs/`, `AGENTS.md` 等)。

```
~/.omo/teams/                                  (drwx------, 已存在)
├── code-review/
│   └── config.json                            (新文件, ~30 行)
├── security-audit/
│   └── config.json                            (新文件, ~30 行)
├── backend-resilience/
│   └── config.json                            (新文件, ~30 行)
└── frontend-quality/
    └── config.json                            (新文件, ~30 行)
```

每个 config.json schema 来自 [team-mode 文档](https://raw.githubusercontent.com/code-yeongyu/oh-my-openagent/dev/docs/guide/team-mode.md):
- `name` (string, required)
- `description` (string, required)
- `lead` (object, `{kind, subagent_type}`)
- `members` (array of `{kind, subagent_type, prompt_append?}`)

`version` / `createdAt` / `leadAgentId` 由 loader 自动填充,无需手写。

---

## Task 1: 确认基础环境

**Files:** 无写入,只检查

- [ ] **Step 1.1: 确认 `~/.omo/` 目录存在且可写**

```bash
ls -la ~/.omo/ | head -10
```

期望输出:`drwx------ ... teams/ ... runtime/ ... worktrees/`(目录列表至少包含 `teams/` 子目录)

- [ ] **Step 1.2: 确认 `teams/` 子目录状态(可能为空或已有内容)**

```bash
ls -la ~/.omo/teams/ 2>/dev/null || echo "teams/ does not exist yet"
```

**如果 `teams/` 不存在**,继续 Task 1.3 创建。
**如果 `teams/` 已存在但为空**,直接进入 Task 1.4。
**如果 `teams/` 已有同名 team**(`code-review` / `security-audit` / `backend-resilience` / `frontend-quality`),备份现有再继续。

- [ ] **Step 1.3: 如果 `teams/` 不存在,创建它**

```bash
mkdir -p ~/.omo/teams
chmod 700 ~/.omo/teams
ls -la ~/.omo/ | grep teams
```

期望:`drwx------ ... teams`

- [ ] **Step 1.4: 备份任何已存在的同名 team(若有冲突)**

```bash
# 仅当 Task 1.2 发现冲突时执行
# 备份位置: ~/.omo/.teams-backup/<timestamp>/<team-name>/, 避免污染 ~/.omo/teams/ 根目录
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
for t in code-review security-audit backend-resilience frontend-quality; do
    if [ -f ~/.omo/teams/$t/config.json ]; then
        mkdir -p ~/.omo/.teams-backup/$TS/$t
        cp ~/.omo/teams/$t/config.json ~/.omo/.teams-backup/$TS/$t/
        echo "backed up: $t -> ~/.omo/.teams-backup/$TS/$t/"
    fi
done
ls -la ~/.omo/.teams-backup/ 2>/dev/null || echo "no backup created (no conflicts)"
```

期望:无冲突时输出 `no backup created (no conflicts)`;有冲突时输出 `backed up: ...`。

- [ ] **Step 1.5: 确认 python3 可用(JSON 验证依赖)**

```bash
python3 --version
```

期望:`Python 3.11+`(`auto_trade` 项目要求)

---

## Task 2: 写入 `code-review` 配置

**Files:** Create `~/.omo/teams/code-review/config.json`

- [ ] **Step 2.1: 创建 team 目录**

```bash
mkdir -p ~/.omo/teams/code-review
chmod 700 ~/.omo/teams/code-review
ls -la ~/.omo/teams/code-review
```

期望:空目录,`drwx------` 权限。

- [ ] **Step 2.2: 写入 config.json**

```bash
cat > ~/.omo/teams/code-review/config.json <<'EOF'
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
EOF
echo "wrote: ~/.omo/teams/code-review/config.json"
```

- [ ] **Step 2.3: 验证 JSON 合法性**

```bash
python3 -c "import json; d = json.load(open('/home/'$HOME'/.omo/teams/code-review/config.json')); assert d['name'] == 'code-review'; assert d['lead']['subagent_type'] == 'sisyphus'; assert len(d['members']) == 3; print('OK: code-review valid')"
```

期望输出:`OK: code-review valid`
如果失败:检查 JSON 格式(`jq` 或 `python3 -m json.tool` 调试)。

- [ ] **Step 2.4: 权限收紧**

```bash
chmod 600 ~/.omo/teams/code-review/config.json
ls -la ~/.omo/teams/code-review/config.json
```

期望:`-rw------- ... config.json`

---

## Task 3: 写入 `security-audit` 配置

**Files:** Create `~/.omo/teams/security-audit/config.json`

- [ ] **Step 3.1: 创建 team 目录**

```bash
mkdir -p ~/.omo/teams/security-audit
chmod 700 ~/.omo/teams/security-audit
```

- [ ] **Step 3.2: 写入 config.json**

```bash
cat > ~/.omo/teams/security-audit/config.json <<'EOF'
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
EOF
```

- [ ] **Step 3.3: 验证 JSON 合法性**

```bash
python3 -c "import json; d = json.load(open('/home/'$HOME'/.omo/teams/security-audit/config.json')); assert d['name'] == 'security-audit'; assert d['lead']['subagent_type'] == 'sisyphus'; assert len(d['members']) == 3; print('OK: security-audit valid')"
```

期望:`OK: security-audit valid`

- [ ] **Step 3.4: 权限收紧**

```bash
chmod 600 ~/.omo/teams/security-audit/config.json
```

---

## Task 4: 写入 `backend-resilience` 配置

**Files:** Create `~/.omo/teams/backend-resilience/config.json`

- [ ] **Step 4.1: 创建 team 目录**

```bash
mkdir -p ~/.omo/teams/backend-resilience
chmod 700 ~/.omo/teams/backend-resilience
```

- [ ] **Step 4.2: 写入 config.json**

```bash
cat > ~/.omo/teams/backend-resilience/config.json <<'EOF'
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
EOF
```

- [ ] **Step 4.3: 验证 JSON 合法性**

```bash
python3 -c "import json; d = json.load(open('/home/'$HOME'/.omo/teams/backend-resilience/config.json')); assert d['name'] == 'backend-resilience'; assert d['lead']['subagent_type'] == 'sisyphus'; assert len(d['members']) == 3; print('OK: backend-resilience valid')"
```

期望:`OK: backend-resilience valid`

- [ ] **Step 4.4: 权限收紧**

```bash
chmod 600 ~/.omo/teams/backend-resilience/config.json
```

---

## Task 5: 写入 `frontend-quality` 配置

**Files:** Create `~/.omo/teams/frontend-quality/config.json`

- [ ] **Step 5.1: 创建 team 目录**

```bash
mkdir -p ~/.omo/teams/frontend-quality
chmod 700 ~/.omo/teams/frontend-quality
```

- [ ] **Step 5.2: 写入 config.json**

```bash
cat > ~/.omo/teams/frontend-quality/config.json <<'EOF'
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
EOF
```

- [ ] **Step 5.3: 验证 JSON 合法性**

```bash
python3 -c "import json; d = json.load(open('/home/'$HOME'/.omo/teams/frontend-quality/config.json')); assert d['name'] == 'frontend-quality'; assert d['lead']['subagent_type'] == 'sisyphus'; assert len(d['members']) == 3; print('OK: frontend-quality valid')"
```

期望:`OK: frontend-quality valid`

- [ ] **Step 5.4: 权限收紧**

```bash
chmod 600 ~/.omo/teams/frontend-quality/config.json
```

---

## Task 6: 批量完整性验证

**Files:** 验证 Task 2-5 写入的 4 个文件

- [ ] **Step 6.1: 批量 JSON 合法性**

```bash
python3 <<'PY'
import json
teams = ['code-review', 'security-audit', 'backend-resilience', 'frontend-quality']
for t in teams:
    p = f'{__import__("os").environ["HOME"]}/.omo/teams/{t}/config.json'
    d = json.load(open(p))
    assert d['name'] == t, f"name mismatch in {t}: {d['name']}"
    assert d['lead']['subagent_type'] == 'sisyphus', f"lead not sisyphus in {t}"
    assert len(d['members']) == 3, f"members != 3 in {t}"
    for m in d['members']:
        assert m['kind'] == 'subagent_type'
        assert m['subagent_type'] in ('hephaestus', 'sisyphus-junior', 'atlas'), f"unexpected member: {m['subagent_type']}"
        assert 'prompt_append' in m, f"missing prompt_append in {t}/{m['subagent_type']}"
print(f'OK: all 4 teams valid')
PY
```

期望:`OK: all 4 teams valid`
如果失败:返回对应 Task 2-5 检查实际写入的 config.json。

- [ ] **Step 6.2: 结构统计一致性**

```bash
echo "subagent_type count per file:"
for t in code-review security-audit backend-resilience frontend-quality; do
    n=$(grep -c '"kind": "subagent_type"' ~/.omo/teams/$t/config.json)
    echo "  $t: $n (expected: 4 = 1 lead + 3 members)"
done
echo ""
echo "sisyphus lead count per file:"
for t in code-review security-audit backend-resilience frontend-quality; do
    n=$(grep -c '"subagent_type": "sisyphus"' ~/.omo/teams/$t/config.json)
    echo "  $t: $n (expected: 1)"
done
```

期望:每个 team 都是 `4` 个 subagent_type + `1` 个 sisyphus lead。

- [ ] **Step 6.3: 目录权限验证**

```bash
ls -la ~/.omo/teams/ | grep -E "code-review|security-audit|backend-resilience|frontend-quality"
```

期望:4 行,每行 `drwx------` 目录 + `-rw-------` config.json(在子目录 ls 中显示)

- [ ] **Step 6.4: 清理备份(可选)**

```bash
# 备份目录在 ~/.omo/.teams-backup/(不在 teams/ 下)
ls -la ~/.omo/.teams-backup/ 2>/dev/null || echo "no backup directory (no conflict in Task 1.4)"
```

期望:列出 `.teams-backup/<timestamp>/` 目录(如 Task 1.4 创建过)。**不删除,保留作为回滚快照。**

---

## Task 7: 运行 `bunx oh-my-opencode doctor` 验证

**Files:** 无写入,只读验证

- [ ] **Step 7.1: 运行 doctor 报告**

```bash
bunx oh-my-opencode doctor 2>&1 | tee /tmp/omo-doctor-$(date +%s).log
```

期望:输出包含 `team-mode` 检查项,显示 4 个 declared teams。
**耗时:** 首次运行可能下载 package,30-60 秒。

- [ ] **Step 7.2: 提取并检查 team-mode 段**

```bash
LOG=$(ls -t /tmp/omo-doctor-*.log | head -1)
echo "=== team-mode section ==="
grep -A 20 -i "team.mode\|team_mode\|declared team" "$LOG" || echo "team-mode section not found in output"
```

期望输出形如:
```
=== team-mode section ===
team-mode:
  tmux available: false
  git available: true
  declared teams: 4
  - code-review
  - security-audit
  - backend-resilience
  - frontend-quality
  active runtime dirs: 0
```

**如果 doctor 报告 `< 4` teams**:回到 Task 2-5 检查对应 config.json 是否成功写入(可能是 mkdir 失败但没有 error)。
**如果 doctor 报告 `0` teams**:检查 `~/.config/opencode/oh-my-openagent.json` 中 `team_mode.enabled` 仍为 `true`(2026-06-03 已配置)。

- [ ] **Step 7.3: 记录 doctor 输出摘要**

```bash
# 提取关键信息到项目 docs(便于回溯)
mkdir -p /home/lcy/code/auto_trade/docs/superpowers/deployment
LOG=$(ls -t /tmp/omo-doctor-*.log | head -1)
cp "$LOG" /home/lcy/code/auto_trade/docs/superpowers/deployment/2026-06-04-omo-teams-doctor.log
echo "doctor log archived: docs/superpowers/deployment/2026-06-04-omo-teams-doctor.log"
```

**注意**:此步骤会向项目仓库添加一个 `deployment/` 目录的文件。**该文件不进 git commit**(deployment 目录是临时归档,可后续 `.gitignore` 或保留)。

---

## Task 8: 重启 OpenCode 并验证 `team_list`

**Files:** 无写入,需用户手动操作

- [ ] **Step 8.1: 提示用户重启 OpenCode**

```
⚠️  Team Mode 需重启 opencode 后生效。请:
1. 当前 opencode TUI 中按 Ctrl+C 退出(或 /exit 命令)
2. 重新运行 `opencode` 启动 TUI
3. 回到本会话继续 Step 8.2
```

**此步骤需用户配合**,等待用户确认重启完成。

- [ ] **Step 8.2: 在重启后的主会话中调用 `team_list`**

```
# 在 Sisyphus 主会话中输入:
/team_list
# 或通过工具调用,期望返回 4 个 team 名称的列表
```

期望输出(简化):
```
[
  {"name": "code-review", "description": "Code quality review..."},
  {"name": "security-audit", "description": "Security audit..."},
  {"name": "backend-resilience", "description": "Backend resilience..."},
  {"name": "frontend-quality", "description": "Frontend quality..."}
]
```

- [ ] **Step 8.3: 如果 `team_list` 缺失或不全**

```bash
# 1. 确认 oh-my-openagent.json 仍有 team_mode.enabled=true
python3 -c "import json; print(json.load(open(__import__('os').environ['HOME']+'/.config/opencode/oh-my-openagent.json'))['team_mode'])"

# 2. 确认 4 个 config.json 存在
ls ~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}/config.json

# 3. 重启 opencode
```

期望:步骤 1 输出 `{'enabled': True, ...}`,步骤 2 列出 4 个文件。
如果步骤 1 异常,需恢复 `team_mode` 块(参考 `~/.config/opencode/oh-my-openagent.json.backup-2026-06-03T18-43-08Z`)。

---

## Task 9: 端到端 dry-run(最小任务)

**Files:** 无写入,功能验证

- [ ] **Step 9.1: 用 `code-review` team 跑一次最小 dry-run**

在 Sisyphus 主会话中:
```
# 通过工具调用
team_create(
    name="code-review",
    task="List the names of the 4 config.json files under ~/.omo/teams/ (no actual review needed)"
)
```

期望:`team_create` 成功,返回一个 team run id。Lead sisyphus 创建 1 个 task 并分派给 3 个 member。Members 并行执行极简任务(只 ls)。

- [ ] **Step 9.2: 监控团队状态**

```
team_status
```

期望:显示 lead + 3 members 的实时状态(active / completed / failed),任务列表至少有 1 个 item。

- [ ] **Step 9.3: 关闭 team**

```
team_shutdown_request
```

期望:收到 member 的 `team_approve_shutdown` 响应。

- [ ] **Step 9.4: 删除 team runtime**

```
team_delete
```

期望:`team_delete` 成功,`~/.omo/runtime/` 不再留该 team 的目录。

- [ ] **Step 9.5: 验证 `team_list` 仍返回 4 teams**

```
team_list
```

期望:仍是 4 个 declared teams(`team_delete` 只清理 runtime,不影响 declared specs)。

---

## Task 10: 跨项目复用验证

**Files:** 创建临时测试目录(完成后删除)

- [ ] **Step 10.1: 创建临时测试目录**

```bash
mkdir -p /tmp/opencode-cross-project-test
cd /tmp/opencode-cross-project-test
git init -q
echo "# Test" > README.md
git add . && git commit -q -m "init"
echo "test dir: $(pwd)"
```

- [ ] **Step 10.2: 在临时目录启动 opencode 并验证 `team_list`**

```
⚠️  此步骤需用户配合:
1. 当前 opencode TUI 中按 Ctrl+C 退出
2. 切到 /tmp/opencode-cross-project-test
3. 运行 opencode
4. 在新会话中调用 team_list
5. 期望返回 4 个 teams(同 Task 8.2)
6. 完成后 Ctrl+C 退出
7. 回到 auto_trade 项目主目录,运行 opencode
```

期望:在任何项目目录启动 opencode,`team_list` 都返回相同的 4 teams(证明 user scope 配置生效)。

- [ ] **Step 10.3: 清理临时目录**

```bash
rm -rf /tmp/opencode-cross-project-test
echo "cleaned: /tmp/opencode-cross-project-test"
```

---

## Task 11: 部署报告与回滚说明

**Files:** 可选,创建部署记录

- [ ] **Step 11.1: 在项目 `docs/` 下创建部署记录**

```bash
REPORT=/home/lcy/code/auto_trade/docs/superpowers/deployment/2026-06-04-omo-teams-deployment.md
mkdir -p $(dirname $REPORT)
cat > $REPORT <<'EOF'
# oh-my-openagent 常驻团队部署记录

- **部署日期:** 2026-06-04
- **部署者:** Sisyphus (via subagent/executing-plans)
- **Spec:** `docs/superpowers/specs/2026-06-04-omo-teams-design.md` (commit `c176c02`)
- **Plan:** `docs/superpowers/plans/2026-06-04-omo-teams-config-deployment.md`
- **部署位置:** `~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}/config.json`
- **配置位置:** `~/.config/opencode/oh-my-openagent.json` (team_mode.enabled=true)
- **备份:** `~/.omo/.teams-backup/<timestamp>/`(如有覆盖,不在 `~/.omo/teams/` 根下,避免 loader 误读)
- **doctor 报告:** `docs/superpowers/deployment/2026-06-04-omo-teams-doctor.log`

## 4 个团队
- `code-review` — 代码质量审查(命名/重复/架构/类型)
- `security-audit` — 安全审计(OWASP/密钥/auth/CVE)
- `backend-resilience` — 后端韧性(重试/对账/锁/断连)
- `frontend-quality` — 前端质量(ai-slop/组件/TS/可访问性)

## 回滚步骤
如需回滚到"仅 team_mode 启用但无 teams"状态:
1. `rm -rf ~/.omo/teams/{code-review,security-audit,backend-resilience,frontend-quality}`
2. `team_mode.enabled` 仍为 true,不影响基础 Team Mode 运行
3. 完整回到无团队状态:`rm -rf ~/.omo/teams && 保留 ~/.omo/runtime/ ~/.omo/worktrees/`

## 已知限制
- `prompt_append` 字段在 TeamSpec 文档中未明确列出;如 team_create 报错,fallback 方案见 spec §4 末尾。
- tmux 未安装,`tmux_visualization: false`,实时 pane 监控不可用。
EOF
echo "report: $REPORT"
```

- [ ] **Step 11.2: 报告完成状态**

报告本 plan 的完成情况:
- ✅ 4 个 config.json 写入 + JSON 验证
- ✅ doctor 报告确认 4 teams
- ✅ 重启 opencode + team_list 返回 4
- ✅ 端到端 dry-run (team_create/status/delete)
- ✅ 跨项目验证通过
- ✅ 部署报告存档

---

## Self-Review Checklist (执行者跑)

执行完所有 task 后,运行以下自检:

```bash
# 1. 文件存在
[ -f ~/.omo/teams/code-review/config.json ] && \
[ -f ~/.omo/teams/security-audit/config.json ] && \
[ -f ~/.omo/teams/backend-resilience/config.json ] && \
[ -f ~/.omo/teams/frontend-quality/config.json ] && echo "OK: 4 files exist"

# 2. JSON 合法
python3 -c "import json,os; [json.load(open(os.environ['HOME']+'/.omo/teams/'+t+'/config.json')) for t in ['code-review','security-audit','backend-resilience','frontend-quality']]; print('OK: 4 JSON valid')"

# 3. 团队数 = 4
ls ~/.omo/teams/*/config.json | wc -l  # 期望: 4

# 4. doctor 报告(可选重跑)
bunx oh-my-opencode doctor 2>&1 | grep -A 5 "team.mode\|team_mode" | head -10
```

如全部 OK,plan 完成;如有失败,回到对应 Task 调试。
