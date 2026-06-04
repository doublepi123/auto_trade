#!/usr/bin/env bash
# omo-teams readiness check (run BEFORE restarting opencode TUI)
# 用途: 在用户重启 opencode TUI 之前,确认文件系统层面一切就绪
# 范围: 11 个 task 的自检 checklist 自动化 (Task 1, 6, 7 部分 + 验证逻辑)
# 不验证: opencode plugin 实际能否识别 4 teams(那需要 Task 8 重启后调 team_list)

set -e

TEAMS=(code-review security-audit backend-resilience frontend-quality)
PASS=0
FAIL=0
WARN=0

ok() { echo "  \033[32m✓\033[0m $1"; PASS=$((PASS+1)); }
fail() { echo "  \033[31m✗\033[0m $1"; FAIL=$((FAIL+1)); }
warn() { echo "  \033[33m!\033[0m $1"; WARN=$((WARN+1)); }

echo "════════════════════════════════════════════════════════════"
echo "  omo-teams Phase 2 Readiness Check"
echo "════════════════════════════════════════════════════════════"
echo ""

# ─── Check 1: ~/.omo/ exists and writable ───
echo "── 1. ~/.omo/ 基础环境 ──"
if [ -d "$HOME/.omo" ] && [ -w "$HOME/.omo" ]; then
    ok "~/.omo/ 存在且可写"
else
    fail "~/.omo/ 不存在或不可写: 跑 mkdir -p ~/.omo"
fi

# ─── Check 2: ~/.omo/teams/ exists ───
echo ""
echo "── 2. ~/.omo/teams/ 目录 ──"
if [ -d "$HOME/.omo/teams" ]; then
    ok "~/.omo/teams/ 存在"
    PERMS=$(stat -c %a "$HOME/.omo/teams" 2>/dev/null)
    if [ "$PERMS" = "700" ]; then
        ok "权限 700 (drwx------)"
    else
        warn "权限 $PERMS (建议 700)"
    fi
else
    fail "~/.omo/teams/ 不存在"
fi

# ─── Check 3: 4 个 team config.json 存在 + JSON 合法 ───
echo ""
echo "── 3. 4 个 team config.json ──"
for t in "${TEAMS[@]}"; do
    f="$HOME/.omo/teams/$t/config.json"
    if [ ! -f "$f" ]; then
        fail "缺失: $f"
        continue
    fi
    # JSON 合法性
    if python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
        ok "$t/config.json 存在 + JSON 合法"
    else
        fail "$t/config.json JSON 非法"
        continue
    fi
    # name 字段
    NAME=$(python3 -c "import json; print(json.load(open('$f'))['name'])" 2>/dev/null)
    if [ "$NAME" = "$t" ]; then
        ok "$t name 字段正确"
    else
        fail "$t name 字段是 '$NAME' (期望 '$t')"
    fi
    # lead 是 sisyphus
    LEAD=$(python3 -c "import json; print(json.load(open('$f'))['lead']['subagent_type'])" 2>/dev/null)
    if [ "$LEAD" = "sisyphus" ]; then
        ok "$t lead = sisyphus"
    else
        fail "$t lead = '$LEAD' (期望 sisyphus)"
    fi
    # 3 个 members
    N=$(python3 -c "import json; print(len(json.load(open('$f'))['members']))" 2>/dev/null)
    if [ "$N" = "3" ]; then
        ok "$t 有 3 个 members"
    else
        fail "$t members 数 = $N (期望 3)"
    fi
    # 每个 member 有 prompt_append
    ALL_APPEND=$(python3 -c "import json; d=json.load(open('$f')); print(all('prompt_append' in m for m in d['members']))" 2>/dev/null)
    if [ "$ALL_APPEND" = "True" ]; then
        ok "$t 所有 member 有 prompt_append"
    else
        warn "$t 某些 member 缺 prompt_append (可能降级)"
    fi
    # 文件权限
    FPERMS=$(stat -c %a "$f" 2>/dev/null)
    if [ "$FPERMS" = "600" ]; then
        ok "$t/config.json 权限 600"
    else
        warn "$t/config.json 权限 $FPERMS (建议 600)"
    fi
done

# ─── Check 4: oh-my-openagent.json team_mode 启用 ───
echo ""
echo "── 4. oh-my-openagent.json team_mode 配置 ──"
OCO="$HOME/.config/opencode/oh-my-openagent.json"
if [ ! -f "$OCO" ]; then
    fail "$OCO 不存在"
else
    ENABLED=$(python3 -c "import json; d=json.load(open('$OCO')); print(d.get('team_mode',{}).get('enabled', False))" 2>/dev/null)
    if [ "$ENABLED" = "True" ]; then
        ok "team_mode.enabled = true"
    else
        fail "team_mode.enabled = $ENABLED (期望 true)"
    fi
    MAX_PAR=$(python3 -c "import json; d=json.load(open('$OCO')); print(d.get('team_mode',{}).get('max_parallel_members', 0))" 2>/dev/null)
    if [ "$MAX_PAR" -ge 4 ]; then
        ok "max_parallel_members = $MAX_PAR (>= 4 支持 3 members + 1 lead)"
    else
        warn "max_parallel_members = $MAX_PAR (3 members + 1 lead = 4, 建议 >= 4)"
    fi
    TMUX=$(python3 -c "import json; d=json.load(open('$OCO')); print(d.get('team_mode',{}).get('tmux_visualization', 'unset'))" 2>/dev/null)
    if [ "$TMUX" = "False" ]; then
        ok "tmux_visualization = false (无 tmux 时合理)"
    elif [ "$TMUX" = "True" ]; then
        if command -v tmux &>/dev/null; then
            ok "tmux_visualization = true + tmux 已装"
        else
            warn "tmux_visualization = true 但 tmux 未装 (功能会降级)"
        fi
    else
        warn "tmux_visualization 未设置 (用 default)"
    fi
fi

# ─── Check 5: opencode 安装与 plugin 加载 ───
echo ""
echo "── 5. opencode + oh-my-openagent plugin ──"
if command -v opencode &>/dev/null; then
    OC_VER=$(opencode --version 2>/dev/null)
    ok "opencode 已装: $OC_VER"
else
    fail "opencode 不在 PATH"
fi
if [ -f "$HOME/.config/opencode/node_modules/oh-my-openagent/package.json" ] || \
   [ -d "$HOME/.config/opencode/node_modules/@ohcode" ]; then
    ok "oh-my-openagent plugin 安装在 ~/.config/opencode/node_modules"
else
    warn "plugin 路径不标准(可能装在 .opencode/ 或全局)"
fi
# 关键: agent/category 列表存在
AGENT_COUNT=$(python3 -c "import json; d=json.load(open('$OCO')); print(len(d.get('agents', {})))" 2>/dev/null || echo 0)
if [ "$AGENT_COUNT" -ge 11 ]; then
    ok "agents 数量 $AGENT_COUNT (>= 11 期望)"
else
    fail "agents 数量 $AGENT_COUNT (< 11, plugin 可能未加载)"
fi

# ─── Check 6: doctor 工具 (npx) ───
echo ""
echo "── 6. doctor 工具 ──"
if command -v npx &>/dev/null; then
    ok "npx 可用 ($(npx --version))"
else
    fail "npx 不在 PATH (无法跑 doctor)"
fi
if command -v bunx &>/dev/null; then
    ok "bunx 可用 ($(bunx --version))"
else
    warn "bunx 不在 PATH (doctor 改用 npx)"
fi

# ─── Summary ───
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Summary: $PASS passed, $FAIL failed, $WARN warnings"
echo "════════════════════════════════════════════════════════════"
echo ""

if [ $FAIL -eq 0 ]; then
    echo "✅ 准备就绪 — 可重启 opencode TUI,然后在主会话中调 team_list"
    echo ""
    echo "  期望 team_list 返回:"
    echo "    - code-review"
    echo "    - security-audit"
    echo "    - backend-resilience"
    echo "    - frontend-quality"
    exit 0
else
    echo "❌ 有 $FAIL 项失败 — 修复后再次跑此脚本"
    exit 1
fi
