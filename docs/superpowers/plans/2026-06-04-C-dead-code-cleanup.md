# C. 死代码最终清理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 系统化扫描并清理未引用函数、未用 import、未用 schema 字段、未用 export；不改变功能；不重构。

**Architecture:** 三阶段流水线：①扫描（vulture / pyflakes / ts-prune / grep）→ ②人工 review 生成 `dead-code-candidates.txt` → ③分批删除 + 每批跑全测验证。

**Tech Stack:** vulture / pyflakes / ts-prune / grep / pytest 9 / basedpyright / vue-tsc

**前置阅读：**
- 母 spec §4.3
- 现有项目 `backend/app` + `frontend/src` 树结构

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Create | `.sisyphus/evidence/task-C-dead-code-candidates.txt` | 扫描候选清单 + 决策记录 |
| Delete | 各种已确认未引用的符号 | 严格按候选清单 |
| Verify | — | 每批删除后跑全测 + pyright + vue-tsc + build |

> **不创建新代码文件**，仅删除已确认的未引用符号。

---

## 任务 1: 扫描后端未引用代码

**Files:** 临时输出（不提交，仅 review 用）

### Step 1.1: 安装扫描工具

```bash
cd backend
pip install vulture pyflakes  # 如未安装
```

- [ ] **Step 1.1**: 安装工具

### Step 1.2: 跑 vulture（后端，未引用函数/变量）

```bash
cd backend
vulture app --min-confidence=80 --sort-by-size > /tmp/vulture-backend.txt 2>&1
head -50 /tmp/vulture-backend.txt
```

预期：输出未引用符号列表（函数、变量、类）。

- [ ] **Step 1.2**: 跑 vulture，记录输出

### Step 1.3: 跑 pyflakes（后端，未用 import）

```bash
cd backend
pyflakes app > /tmp/pyflakes-backend.txt 2>&1
head -50 /tmp/pyflakes-backend.txt
```

预期：输出未用 import 列表。

- [ ] **Step 1.3**: 跑 pyflakes

### Step 1.4: 跑 schema 字段使用检查

```bash
cd backend
# 提取所有 schema 字段名
grep -E "^\s+\w+:" app/schemas.py | awk -F: '{print $1}' | tr -d ' ' | sort -u > /tmp/schema-fields.txt
# 提取所有引用
grep -rh -oE "(\.|\b)([a-z_]+)\b" app/ | grep -F -f /tmp/schema-fields.txt -w | sort -u > /tmp/schema-fields-used.txt
# 未使用的字段
comm -23 /tmp/schema-fields.txt /tmp/schema-fields-used.txt > /tmp/schema-fields-unused.txt
head -30 /tmp/schema-fields-unused.txt
```

> ⚠️ 此步骤是启发式；可能误报。人工 review 候选清单。

- [ ] **Step 1.4**: 跑 schema 字段检查

### Step 1.5: 跑 tests + frontend 引用双向检查

```bash
cd backend
# 每个候选符号在 tests 和 frontend 中双向确认
for sym in $(cat /tmp/vulture-backend.txt | awk '{print $NF}' | head -20); do
    count=$(grep -r "$sym" app/ tests/ ../frontend/src 2>/dev/null | wc -l)
    echo "$sym: $count references"
done
```

预期：候选符号的引用计数。若某符号仅 1 处定义、0 处引用 → 安全删除。

- [ ] **Step 1.5**: 双向确认

---

## 任务 2: 扫描前端未引用代码

**Files:** 临时输出

### Step 2.1: 安装 ts-prune

```bash
cd frontend
npm install --no-save ts-prune
```

- [ ] **Step 2.1**: 安装 ts-prune

### Step 2.2: 跑 ts-prune

```bash
cd frontend
npx ts-prune > /tmp/ts-prune-frontend.txt 2>&1
head -50 /tmp/ts-prune-frontend.txt
```

预期：未使用的 TypeScript export 列表。

- [ ] **Step 2.2**: 跑 ts-prune

### Step 2.3: 跑前端的 pyflakes 类似检查

```bash
cd frontend
# 找未在 .vue 模板中使用的 import
grep -rE "^import" src/ | head -30
# 找未在 <script setup> 中使用的 ref
# （复杂，跳过；由 ai-slop-remover 在 F 任务处理）
```

- [ ] **Step 2.3**: 跑前端 import 扫描

---

## 任务 3: 合并候选清单 + 人工 review

### Step 3.1: 合并输出到候选清单

```bash
cat > .sisyphus/evidence/task-C-dead-code-candidates.txt << 'EOF'
# 死代码候选清单 (Task C)
# 生成时间: <YYYY-MM-DD>
# 决策原则: 仅删除 ✅ 列出的符号；❌ 列出的保留

## 后端 vulture 候选
$(cat /tmp/vulture-backend.txt)

## 后端 pyflakes 候选
$(cat /tmp/pyflakes-backend.txt)

## 后端 schema 字段未用
$(cat /tmp/schema-fields-unused.txt)

## 前端 ts-prune 候选
$(cat /tmp/ts-prune-frontend.txt)

## 双向确认结果
<按符号逐个列：定义位置 / 引用计数 / 删除决策（✅ 删 / ❌ 保留 原因）>
EOF
```

- [ ] **Step 3.1**: 合并候选清单

### Step 3.2: 人工 review 每个候选

**删除决策标准：**
- ✅ **可删：** 私有函数无引用 / 未用 import / 未用 schema 字段（且不在公开 API 中）/ 未用常量
- ❌ **不可删：** 公共 API（router 注册）/ `__init__.py` 中 export / 注释 / 文档字符串 / 测试中引用 / 配置驱动

逐符号标记决策。

- [ ] **Step 3.2**: 人工 review 决策完成

---

## 任务 4: 分批删除 + 每批全测验证

### Step 4.1: 第 1 批删除（低风险：未用 import）

```bash
# 删除未用 import（每批 ≤5 个文件）
# 示例（实际以候选清单为准）：
# 编辑 backend/app/services/xxx.py，删除未用的 `from typing import Dict` 等
```

- [ ] **Step 4.1**: 删第 1 批

### Step 4.2: 跑全测验证

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
```

预期：全绿。

- [ ] **Step 4.2**: 验证

### Step 4.3: 第 2 批删除（中风险：未用 schema 字段）

```bash
# 删除 schemas.py 中未用字段
# 注意：可能影响 OpenAPI 文档；保持向后兼容（不删 alias 字段）
```

- [ ] **Step 4.3**: 删第 2 批

### Step 4.4: 跑全测验证

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 4.4**: 验证

### Step 4.5: 第 3 批删除（高风险：未引用私有函数 / 未用前端 export）

```bash
# 删除未引用函数 + 前端未用 export
```

- [ ] **Step 4.5**: 删第 3 批

### Step 4.6: 跑全测验证

```bash
cd backend && python3 -m pytest tests/ -v
cd backend && python3 -m basedpyright
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 4.6**: 最终验证

---

## 任务 5: 写证据 + Commit

### Step 5.1: 写证据文件

```bash
cat >> .sisyphus/evidence/task-C-dead-code-candidates.txt << 'EOF'

## 删除执行记录
- 第 1 批（未用 import）：N 处
- 第 2 批（未用 schema 字段）：N 处
- 第 3 批（未引用函数 + 前端 export）：N 处
- 总计删除：N 符号 / M 文件
EOF
```

- [ ] **Step 5.1**: 写证据

### Step 5.2: Commit

```bash
git add -A  # 包含删除的文件
git add .sisyphus/evidence/task-C-dead-code-candidates.txt
git commit -m "chore(cleanup): remove dead code per vulture/pyflakes/ts-prune scan"
```

> ⚠️ **不主动 commit**。agent 输出 "Task C complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 扫描工具已跑（vulture / pyflakes / ts-prune / schema 字段）
- [ ] 候选清单 `.sisyphus/evidence/task-C-dead-code-candidates.txt` 已写，含逐符号决策
- [ ] 删除分 ≥3 批，每批后全测全绿
- [ ] 现有 80+ Cypress spec 全部通过
- [ ] `basedpyright` 0 errors / 0 warnings
- [ ] **不主动 commit**

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| 误删公开 API | 删除前双向 grep + 决策清单；router 注册过的函数 ❌ 保留 |
| 误删 schema 字段影响 OpenAPI | 仅删未引用字段；不删 alias / 兼容字段 |
| 误删动态引用（如 getattr） | vulture 默认 80% 置信度，保守；动态场景需要人工 review |
| 前端误删全局类型 | ts-prune 不识别 `*.d.ts` 全局声明，**保留** `types/index.ts` |
| 误删后回滚 | `git reflog` + `git revert` |

## 范围外（YAGNI）

- ❌ 重构（仅删除，不重命名/拆分）
- ❌ 删除公共 API
- ❌ 删除 `__init__.py` 中 export
- ❌ 删除注释 / 文档字符串
- ❌ 改变功能

---

**Plan C 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.3。**
