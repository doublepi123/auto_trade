<template>
  <el-dialog
    v-model="open"
    :show-close="false"
    width="560px"
    class="command-palette-dialog"
    append-to-body
    destroy-on-close
    align-center
    @open="onOpen"
    data-testid="command-palette-dialog"
  >
    <div class="command-palette" data-testid="command-palette">
      <el-input
        ref="inputRef"
        v-model="query"
        placeholder="输入命令或页面名称…（↑↓ 选择，Enter 执行，Esc 关闭）"
        data-testid="command-palette-input"
        @keydown="onKeydown"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <ul class="command-list" data-testid="command-list">
        <li
          v-for="(cmd, i) in filtered"
          :key="cmd.id"
          :class="['command-item', { active: i === activeIndex }]"
          :data-testid="`command-item-${cmd.id}`"
          @mouseenter="activeIndex = i"
          @click="run(cmd)"
        >
          <span class="command-label">{{ cmd.label }}</span>
          <el-tag size="small" type="info" effect="plain">{{ cmd.group }}</el-tag>
        </li>
        <li v-if="filtered.length === 0" class="command-empty" data-testid="command-empty">
          无匹配命令
        </li>
      </ul>
      <p class="command-hint">Cmd / Ctrl + K 打开</p>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import {
  activateKillSwitch,
  disableKillSwitch,
  getStrategy,
  getWatchlist,
  pauseTrading,
  resumeTrading,
  startTrading,
  stopTrading,
} from '../api'
import { useCommandPalette } from '../composables/useCommandPalette'
import { useConnectionHealth } from '../composables/useConnectionHealth'
import { useSymbolStore } from '../composables/useSymbolStore'
import { useRecentPages } from '../composables/useRecentPages'
import { useViewRefreshRegistry } from '../composables/useViewRefreshRegistry'
import { resolveErrorMessage } from '../utils/error'

interface Command {
  id: string
  label: string
  group: string
  keywords?: string
  run: () => void | Promise<void>
}

const router = useRouter()
const { open, query, activeIndex, recentIds, recordRecent, closePalette } = useCommandPalette()
const { reconnectNow, refreshNow } = useConnectionHealth()
const { requestSymbol } = useSymbolStore()
const { recencyRank } = useRecentPages()
const { currentRefresh } = useViewRefreshRegistry()
const inputRef = ref<{ focus: () => void } | null>(null)
const symbols = ref<string[]>([])
let symbolsLoaded = false

// Lazily index known symbols (primary strategy + watchlist) so the palette can
// offer a per-symbol "view on dashboard" jump. Fetched on first open.
async function loadSymbols(): Promise<void> {
  if (symbolsLoaded) return
  symbolsLoaded = true
  try {
    const [strategyResp, watchlist] = await Promise.all([getStrategy(), getWatchlist()])
    const set = new Set<string>()
    if (strategyResp.symbol) set.add(strategyResp.symbol)
    for (const item of watchlist) {
      if (item.symbol) set.add(item.symbol)
    }
    symbols.value = [...set]
  } catch {
    // Non-fatal: symbol commands simply won't appear.
  }
}

const NAV_ITEMS: Array<{ id: string; label: string; path: string }> = [
  { id: 'nav-dashboard', label: '仪表盘', path: '/' },
  { id: 'nav-watchlist', label: '观察列表', path: '/watchlist' },
  { id: 'nav-review', label: '复盘', path: '/review' },
  { id: 'nav-reports', label: '交易报告', path: '/reports' },
  { id: 'nav-backtest', label: '回测', path: '/backtest' },
  { id: 'nav-experiments', label: '策略实验', path: '/experiments' },
  { id: 'nav-strategy', label: '策略配置', path: '/strategy' },
  { id: 'nav-credentials', label: '凭证设置', path: '/credentials' },
  { id: 'nav-history', label: '交易历史', path: '/history' },
  { id: 'nav-events', label: '决策时间线', path: '/events' },
  { id: 'nav-alerts', label: '告警规则', path: '/alerts' },
  { id: 'nav-notifications', label: '通知中心', path: '/notifications' },
  { id: 'nav-lab', label: '优化工作台', path: '/lab' },
]

async function runControl(
  action: () => Promise<unknown>,
  okMsg: string,
  confirm?: string,
): Promise<void> {
  if (confirm) {
    try {
      await ElMessageBox.confirm(confirm, '请确认', { type: 'warning' })
    } catch {
      return
    }
  }
  try {
    await action()
    ElMessage.success(okMsg)
    refreshNow()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '操作失败'))
  }
}

const commands = computed<Command[]>(() => {
  const nav: Command[] = NAV_ITEMS.map((n) => ({
    id: n.id,
    label: `前往：${n.label}`,
    group: '页面导航',
    keywords: `go navigate ${n.label}`,
    run: () => {
      void router.push(n.path)
    },
  }))
  const control: Command[] = [
    {
      id: 'ctrl-start',
      label: '启动运行',
      group: '操作控制',
      keywords: 'start run begin',
      run: () => runControl(startTrading, '已启动运行'),
    },
    {
      id: 'ctrl-stop',
      label: '停止运行',
      group: '操作控制',
      keywords: 'stop halt',
      run: () => runControl(stopTrading, '已停止运行', '确定停止运行？'),
    },
    {
      id: 'ctrl-pause',
      label: '暂停交易',
      group: '操作控制',
      keywords: 'pause hold',
      run: () => runControl(pauseTrading, '已暂停'),
    },
    {
      id: 'ctrl-resume',
      label: '恢复交易',
      group: '操作控制',
      keywords: 'resume continue',
      run: () => runControl(resumeTrading, '已恢复'),
    },
    {
      id: 'ctrl-kill',
      label: '紧急停止（Kill Switch）',
      group: '操作控制',
      keywords: 'kill switch emergency',
      run: () => runControl(activateKillSwitch, '已触发紧急停止', '确定触发紧急停止？'),
    },
    {
      id: 'ctrl-disable-kill',
      label: '解除紧急停止',
      group: '操作控制',
      keywords: 'disable kill release',
      run: () => runControl(disableKillSwitch, '已解除紧急停止'),
    },
  ]
  const utility: Command[] = [
    {
      id: 'util-reconnect',
      label: '重新连接实时行情',
      group: '工具',
      keywords: 'reconnect websocket refresh',
      run: () => {
        reconnectNow()
        refreshNow()
        ElMessage.success('已发起重连')
      },
    },
  ]
  // "Refresh current page" only appears when the active view registered a
  // reload function (Dashboard / Watchlist / Reports).
  const refreshCmds: Command[] = currentRefresh.value
    ? [
        {
          id: 'util-refresh-view',
          label: '刷新当前页',
          group: '工具',
          keywords: 'reload refresh page',
          run: () => {
            const fn = currentRefresh.value
            if (fn) {
              try {
                void fn()
              } catch (e: unknown) {
                ElMessage.error(resolveErrorMessage(e, '刷新失败'))
              }
            }
          },
        },
      ]
    : []
  const symbolCmds: Command[] = symbols.value.map((sym) => ({
    id: `symbol-${sym}`,
    label: `在仪表盘查看 ${sym}`,
    group: '标的',
    keywords: `symbol chart view ${sym}`,
    run: () => {
      requestSymbol(sym)
      void router.push('/')
    },
  }))
  return [...nav, ...control, ...symbolCmds, ...utility, ...refreshCmds]
})

function scoreCommand(cmd: Command, q: string): number | null {
  if (!q) return 0
  const hay = `${cmd.label} ${cmd.keywords ?? ''} ${cmd.group}`.toLowerCase()
  const idx = hay.indexOf(q)
  if (idx === -1) return null
  const labelIdx = cmd.label.toLowerCase().indexOf(q)
  let score = 100 - idx
  if (labelIdx === 0) score += 50
  else if (labelIdx > 0) score += 20
  return score
}

const filtered = computed<Command[]>(() => {
  const all = commands.value
  const q = query.value.trim().toLowerCase()
  if (!q) {
    // Order by combined recency: nav commands by actual page-visit recency
    // (more accurate than command-use), other commands by command-use recency.
    const navPath = (id: string) => NAV_ITEMS.find((n) => n.id === id)?.path ?? ''
    const rank = (cmd: Command): number => {
      if (cmd.id.startsWith('nav-')) return recencyRank(navPath(cmd.id))
      const idx = recentIds.value.indexOf(cmd.id)
      return idx === -1 ? Number.MAX_SAFE_INTEGER : idx
    }
    return [...all].sort((a, b) => rank(a) - rank(b))
  }
  return all
    .map((c) => ({ c, s: scoreCommand(c, q) as number | null }))
    .filter((x) => x.s !== null)
    .sort((a, b) => (b.s as number) - (a.s as number))
    .map((x) => x.c)
})

watch(filtered, () => {
  if (activeIndex.value >= filtered.value.length) activeIndex.value = 0
})
watch(query, () => {
  activeIndex.value = 0
})

function run(cmd: Command): void {
  recordRecent(cmd.id)
  closePalette()
  cmd.run()
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === 'ArrowDown') {
    ev.preventDefault()
    activeIndex.value = Math.min(activeIndex.value + 1, filtered.value.length - 1)
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault()
    activeIndex.value = Math.max(activeIndex.value - 1, 0)
  } else if (ev.key === 'Enter') {
    ev.preventDefault()
    const cmd = filtered.value[activeIndex.value]
    if (cmd) run(cmd)
  }
}

function onOpen(): void {
  query.value = ''
  activeIndex.value = 0
  void loadSymbols()
  nextTick(() => inputRef.value?.focus())
}
</script>

<style scoped>
.command-palette-dialog :deep(.el-dialog__header) {
  display: none;
}
.command-palette-dialog :deep(.el-dialog__body) {
  padding: 0;
}
.command-palette {
  padding: 12px;
}
.command-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}
.command-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
.command-item.active {
  background: #ecf5ff;
}
.command-item .command-label {
  color: #303133;
}
.command-empty {
  padding: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
.command-hint {
  margin: 8px 2px 0;
  color: #c0c4cc;
  font-size: 12px;
}
</style>
