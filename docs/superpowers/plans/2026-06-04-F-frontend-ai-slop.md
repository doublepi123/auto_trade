# F. 前端 ai-slop 清理 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 系统化清理前端代码异味（重复组件、魔术字符串、缺失 testid、未用 ref、过度复杂 setup、Vue 3 反模式），不改变功能。

**Architecture:** 调 `ai-slop-remover` 技能逐文件扫描 → 输出 diff → 人工 review → 落盘。每文件独立 review，不批量改。

**Tech Stack:** ai-slop-remover 技能 / Vue 3 / TypeScript / Element Plus 2.8 / Cypress 15

**前置阅读：**
- 母 spec §4.6
- 现有 `frontend/src/**/*.{ts,vue}` 树

---

## 文件结构

| 操作 | 路径 | 职责 |
|------|------|------|
| Modify | 各种前端文件（按扫描结果） | 去异味：重命名、提取、删除未用 |
| Create | `.sisyphus/evidence/task-F-ai-slop-diff.md` | 每个文件 diff 决策记录 |

> **不创建新组件**（除非必要提取 composable），仅清理既有文件。

---

## 任务 1: 扫描 + 候选清单

**Files:**
- Create: `.sisyphus/evidence/task-F-ai-slop-candidates.md`

### Step 1.1: 列出所有前端文件

```bash
cd frontend
find src/ -name "*.vue" -o -name "*.ts" | grep -v "node_modules" | sort > /tmp/fe-files.txt
wc -l /tmp/fe-files.txt
```

预期：~50-80 个文件。

- [ ] **Step 1.1**: 列出文件

### Step 1.2: 逐文件调用 ai-slop-remover

```bash
cd frontend
# 逐文件扫描（人工 review 模式）
while IFS= read -r file; do
    echo "=== Scanning: $file ==="
    # 调用 ai-slop-remover（dry-run / report 模式）
    # 输出"ai-slop-remover $file --report"
done < /tmp/fe-files.txt
```

> 实际调用方式取决于 ai-slop-remover 技能的具体接口（详见 `/remove-ai-slops` 命令）。

- [ ] **Step 1.2**: 扫描

### Step 1.3: 收集候选清单到证据文件

```bash
cat > .sisyphus/evidence/task-F-ai-slop-candidates.md << 'EOF'
# 前端 ai-slop 候选清单 (Task F)
# 生成时间: <YYYY-MM-DD>

## 重复组件逻辑（待提取 composable）
- <file1>: <重复模式>
- <file2>: <重复模式>

## 魔术字符串（待提取常量）
- <file>: <status/event_type 字符串>

## 缺失 data-testid
- <file>: <关键交互元素列表>

## 未使用 ref / computed
- <file>: <未用 ref>

## 过度复杂 <script setup>
- <file>: <行数 + 拆分建议>

## Vue 3 反模式
- <file>: <mutating props / setup 外改 reactive>
EOF
```

- [ ] **Step 1.3**: 写候选清单

---

## 任务 2: 逐文件清理 + 人工 review

### Step 2.1: 第 1 批（重复组件 → 提取 composable）

**规则：≥2 处类似代码块 → 提取**

```typescript
// frontend/src/composables/useFoo.ts（新文件，如适用）
export function useFoo() {
    const state = ref(...);
    // 提取的共享逻辑
    return { state };
}
```

修改 2+ 个使用方调用新 composable。

- [ ] **Step 2.1**: 提取 composable

### Step 2.2: 跑 type-check + build 验证

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

预期：exit 0。

- [ ] **Step 2.2**: 验证

### Step 2.3: 第 2 批（魔术字符串 → 常量）

**示例：**

```typescript
// 之前：
if (event.event_type === "ORDER_FILLED") { ... }
if (status === "running") { ... }

// 之后（frontend/src/utils/constants.ts）：
export const EVENT_TYPE = {
    ORDER_FILLED: "ORDER_FILLED",
    // ...
} as const;

export const RUNNER_STATUS = {
    RUNNING: "running",
    // ...
} as const;
```

替换所有引用。

- [ ] **Step 2.3**: 提取常量

### Step 2.4: 跑验证

```bash
cd frontend && npm run type-check
cd frontend && npm run build
```

预期：exit 0。

- [ ] **Step 2.4**: 验证

### Step 2.5: 第 3 批（缺失 testid → 添加）

**关键交互元素：**
- 按钮（Start/Stop/Pause/Kill Switch）
- 表格行（订单、事件、Watchlist）
- 表单输入（Strategy 配置）
- 通知 Toast 容器（为 P23 任务铺垫）

```vue
<el-button data-testid="dashboard-start-btn" @click="start">Start</el-button>
```

- [ ] **Step 2.5**: 添加 testid

### Step 2.6: 跑验证

```bash
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：现有 80+ Cypress spec 全绿。

- [ ] **Step 2.6**: 验证（含 Cypress）

### Step 2.7: 第 4 批（未用 ref / computed → 删除）

```bash
cd frontend
# 检查 .vue 文件中所有 ref / computed 定义
grep -rE "const \w+ = ref\(" src/ | head -20
# 对每个 ref 确认是否在 template 或其他 ref 中使用
```

- [ ] **Step 2.7**: 删未用 ref

### Step 2.8: 跑验证

```bash
cd frontend && npm run type-check
cd frontend && npm run build
cd frontend && npm run cypress:run
```

预期：全绿。

- [ ] **Step 2.8**: 验证

---

## 任务 3: 写 diff 记录 + Commit

### Step 3.1: 写 diff 决策记录

```bash
cat > .sisyphus/evidence/task-F-ai-slop-diff.md << 'EOF'
# 前端 ai-slop 清理 diff 记录 (Task F)
# 生成时间: <YYYY-MM-DD>

## 第 1 批：composable 提取
- 新建 frontend/src/composables/useFoo.ts
- 修改 <file1>: 调用 useFoo
- 修改 <file2>: 调用 useFoo
- 验证: type-check / build / cypress 全绿

## 第 2 批：魔术字符串 → 常量
- 新建/修改 frontend/src/utils/constants.ts
- 修改 <file1>: import EVENT_TYPE
- ...
- 验证: type-check / build / cypress 全绿

## 第 3 批：testid 添加
- 修改 <file1>: 添加 data-testid="..."
- ...
- 验证: cypress 现有 spec 全绿

## 第 4 批：未用 ref 删除
- 修改 <file1>: 删 ref
- ...
- 验证: type-check / build / cypress 全绿
EOF
```

- [ ] **Step 3.1**: 写 diff 记录

### Step 3.2: Commit

```bash
git add -A
git add .sisyphus/evidence/task-F-ai-slop-candidates.md .sisyphus/evidence/task-F-ai-slop-diff.md
git commit -m "refactor(frontend): ai-slop cleanup (composables, constants, testids)"
```

> ⚠️ **不主动 commit**。agent 输出 "Task F complete, ready for commit (awaiting user approval)"。

---

## 验证清单

- [ ] 候选清单 + diff 记录已写
- [ ] 4 批清理分批完成，每批全测全绿
- [ ] 现有 80+ Cypress spec 全部通过
- [ ] `vue-tsc` 0 errors
- [ ] `npm run build` exit 0
- [ ] **不主动 commit**

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| ai-slop 误改功能 | 人工 review 每个文件 diff；不修改 props/refs/事件流 |
| 提取 composable 改变行为 | 提取前后行为对照；逐个 Cypress spec 验证 |
| 常量提取遗漏引用 | grep 双向确认 + Cypress 跑通 |
| 误删未用 ref 实际动态用 | 在 `<script setup>` 顶部注释哪些 ref 是动态 ref（如 `useStore` 返回） |

## 范围外（YAGNI）

- ❌ 重写组件逻辑
- ❌ 修改 props / events / 状态结构
- ❌ 修改 UI 视觉
- ❌ 创建新页面
- ❌ 添加新依赖

---

**Plan F 结束。Spec 母文档：`docs/superpowers/specs/2026-06-04-tech-debt-p23-design.md` §4.6。**
