<template>
  <div class="watchlist-page">
    <div class="page-heading">
      <h3>观察列表</h3>
      <p>多标的行情观察（仅观察，不自动下单）</p>
    </div>

    <el-card style="margin-bottom: 20px">
      <el-form :inline="true" @submit.prevent="handleAdd">
        <el-form-item label="股票代码">
          <el-input v-model="newSymbol" placeholder="例如 AAPL.US" style="width: 180px" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="newMarket">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="别名">
          <el-input v-model="newAlias" placeholder="可选别名" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="adding" :disabled="!newSymbol.trim()" @click="handleAdd">
            添加
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="addError" style="margin-top: 8px">
        <el-alert :title="addError" type="error" :closable="false" show-icon />
      </div>
    </el-card>

    <el-card>
      <div class="watchlist-toolbar">
        <span class="watchlist-toolbar-note">{{ items.length }} 个标的 · 行情每 15s 刷新</span>
        <span class="watchlist-toolbar-note" data-testid="watchlist-last-refresh">行情最近成功刷新：{{ lastRefreshLabel }}</span>
        <el-button size="small" plain data-testid="watchlist-refresh-now" @click="refreshNow">手动刷新</el-button>
        <el-button
          size="small"
          plain
          :disabled="items.length === 0"
          data-testid="watchlist-export-csv"
          @click="exportSnapshot"
        >
          导出快照 CSV
        </el-button>
      </div>
      <div class="watchlist-filters">
        <el-input v-model="searchText" placeholder="搜索代码/别名" clearable style="width: 180px" data-testid="watchlist-search" data-view-search="true" />
        <el-select v-model="marketFilter" placeholder="全部市场" clearable style="width: 120px" data-testid="watchlist-market-filter">
          <el-option label="US" value="US" />
          <el-option label="HK" value="HK" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 130px" data-testid="watchlist-status-filter">
          <el-option label="交易中" value="active" />
          <el-option label="观察中" value="watching" />
        </el-select>
        <el-select v-model="scoreBucket" placeholder="全部评分" clearable style="width: 140px" data-testid="watchlist-score-filter">
          <el-option label="高分 ≥70" value="high" />
          <el-option label="中分 40-69" value="mid" />
          <el-option label="低分 <40" value="low" />
        </el-select>
        <el-select v-model="sortMode" style="width: 150px" data-testid="watchlist-sort-mode">
          <el-option label="默认排序" value="default" />
          <el-option label="评分从高到低" value="score_desc" />
          <el-option label="价差从小到大" value="spread_asc" />
          <el-option label="最新价从高到低" value="price_desc" />
        </el-select>
        <el-button :type="hideStaleScores ? 'primary' : ''" data-testid="watchlist-hide-stale" @click="hideStaleScores = !hideStaleScores">隐藏过期评分</el-button>
        <el-button data-testid="watchlist-clear-filters" @click="clearFilters">清空筛选</el-button>
      </div>
      <div class="watchlist-filter-summary" data-testid="watchlist-filter-summary">
        当前显示 {{ filteredItems.length }}/{{ items.length }}
        <span v-if="searchText.trim()"> · 搜索 {{ searchText.trim() }}</span>
        <span v-if="marketFilter"> · 市场 {{ marketFilter }}</span>
        <span v-if="statusFilter"> · 状态 {{ statusFilter === 'active' ? '交易中' : '观察中' }}</span>
        <span v-if="scoreBucket"> · 评分 {{ scoreBucketLabel }}</span>
        <span v-if="sortMode !== 'default'"> · 排序 {{ sortModeLabel }}</span>
        <span v-if="hideStaleScores"> · 隐藏过期评分</span>
      </div>
      <div class="watchlist-bulk-actions">
        <el-checkbox :model-value="allFilteredSelected" data-testid="watchlist-select-all" @change="toggleSelectAll">全选当前结果</el-checkbox>
        <span data-testid="watchlist-selection-summary">已选择 {{ selectedIds.length }}</span>
        <el-button size="small" :disabled="selectedRows.length === 0" data-testid="watchlist-bulk-export" @click="exportSelected">导出所选 CSV</el-button>
        <el-button size="small" type="danger" :disabled="selectedRows.length === 0" data-testid="watchlist-bulk-delete" @click="bulkDeleteDialog = true">批量删除</el-button>
      </div>
      <el-table :data="filteredItems" v-loading="loading" style="width: 100%" data-testid="watchlist-table">
        <el-table-column width="48">
          <template #default="{ row }">
            <el-checkbox :model-value="selectedIds.includes(row.id)" @change="toggleSelection(row.id)" />
          </template>
        </el-table-column>
        <el-table-column prop="symbol" label="代码" width="120" />
        <el-table-column prop="market" label="市场" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.market }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alias" label="别名" width="140">
          <template #default="{ row }">{{ row.alias || '-' }}</template>
        </el-table-column>
        <el-table-column
          label="LLM 评分"
          width="160"
          sortable
          :sort-by="(row: WatchlistItem) => scoreMap[row.symbol]?.score ?? -1"
        >
          <template #default="{ row }">
            <div v-if="scoreMap[row.symbol]">
              <el-tag
                :type="scoreTagType(scoreMap[row.symbol].score)"
                size="small"
                class="score-tag"
                data-testid="watchlist-score-tag"
                @click="openScoreDrawer(scoreMap[row.symbol])"
              >
                {{ scoreMap[row.symbol].score.toFixed(0) }}
              </el-tag>
              <small style="color: #909399; margin-left: 6px">
                {{ scoreActionLabel(scoreMap[row.symbol].recommended_action) }}
              </small>
              <el-tag
                v-if="scoreMap[row.symbol].is_stale"
                type="warning"
                size="small"
                data-testid="watchlist-stale-badge"
                style="margin-left: 6px"
              >
                评分过期
              </el-tag>
            </div>
            <div v-else style="color: #909399">未评分</div>
          </template>
        </el-table-column>
        <el-table-column label="行情" width="180">
          <template #default="{ row }">
            <div v-if="quoteMap[row.symbol]">
              <div>{{ formatCurrency(quoteMap[row.symbol].last_price, row.market) }}</div>
              <small style="color: #909399">
                Bid {{ formatCurrency(quoteMap[row.symbol].bid, row.market) }} / Ask {{ formatCurrency(quoteMap[row.symbol].ask, row.market) }}
              </small>
            </div>
            <div v-else style="color: #909399">-</div>
          </template>
        </el-table-column>
        <el-table-column
          label="价差"
          width="110"
          :sort-method="(a: WatchlistItem, b: WatchlistItem) => (spreadFor(a) ?? -1) - (spreadFor(b) ?? -1)"
          sortable
        >
          <template #default="{ row }">
            <span v-if="quoteMap[row.symbol] && spreadFor(row) !== null" data-testid="watchlist-spread">
              {{ formatCurrency(spreadFor(row), row.market) }}
            </span>
            <span v-else style="color: #909399">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success">交易中</el-tag>
            <el-tag v-else type="info">观察中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              size="small"
              data-testid="watchlist-copy-symbol"
              @click="copySymbol(row.symbol)"
            >复制</el-button>
            <el-button
              v-if="!row.is_active"
              type="primary"
              size="small"
              :loading="activatingId === row.id"
              @click="handleActivate(row.id)"
            >
              设为交易
            </el-button>
            <el-button
              size="small"
              :loading="scoringSymbol === row.symbol"
              :aria-label="`对 ${row.symbol} 进行 LLM 评分`"
              @click="handleScore(row.symbol, row.market)"
            >
              LLM 评分
            </el-button>
            <el-button
              type="danger"
              size="small"
              :loading="removingId === row.id"
              @click="handleRemove(row.id)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="items.length > 0 && filteredItems.length === 0 && !loading" data-testid="watchlist-filter-empty" style="text-align: center; color: #909399; padding: 32px">
        没有匹配的观察标的，请调整筛选条件
      </div>
      <DataState
        v-if="items.length === 0 && !loading"
        empty
        empty-text="暂无观察标的，请添加股票代码"
      />
    </el-card>

    <el-dialog v-model="bulkDeleteDialog" title="确认批量删除" width="360px">
      <p>将删除当前可见已选的 {{ selectedRows.length }} 个观察标的。</p>
      <p class="bulk-delete-symbols">{{ selectedRows.map((row) => row.symbol).join(', ') }}</p>
      <template #footer>
        <el-button @click="bulkDeleteDialog = false">取消</el-button>
        <el-button type="danger" data-testid="watchlist-bulk-delete-confirm" @click="confirmBulkDelete">确认删除</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="scoreDrawer.visible"
      :title="`${scoreDrawer.score?.symbol || ''} LLM 评分详情`"
      size="380px"
      destroy-on-close
      data-testid="watchlist-score-drawer"
    >
      <template v-if="scoreDrawer.score">
        <div class="score-detail-header">
          <div class="score-detail-score" :class="scoreDetailClass">{{ scoreDrawer.score.score.toFixed(0) }}</div>
          <el-tag :type="scoreTagType(scoreDrawer.score.score)" size="small">{{ scoreActionLabel(scoreDrawer.score.recommended_action) }}</el-tag>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">评分依据</div>
          <p class="score-detail-rationale">{{ scoreDrawer.score.rationale || '暂无说明' }}</p>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">置信度</div>
          <strong>{{ (scoreDrawer.score.confidence * 100).toFixed(0) }}%</strong>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">来源</div>
          <el-tag :type="scoreDrawer.score.source.startsWith('fallback') ? 'info' : 'success'" size="small">
            {{ scoreDrawer.score.source }}
          </el-tag>
          <el-tag v-if="scoreDrawer.score.is_stale" type="warning" size="small">已过期</el-tag>
        </div>
        <div class="score-detail-section">
          <div class="score-detail-label">时间</div>
          <div>生成：{{ formatDateTime(scoreDrawer.score.created_at) }}</div>
          <div>过期：{{ formatDateTime(scoreDrawer.score.expires_at) }}</div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { WatchlistItem, WatchlistQuote } from '../types'
import {
  getWatchlist,
  addWatchlistItem,
  removeWatchlistItem,
  activateWatchlistItem,
  getWatchlistQuotes,
  getWatchlistScores,
  scoreWatchlistSymbol,
  type WatchlistScore,
} from '../api/watchlist'
import { formatCurrency } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'
import DataState from '../components/DataState.vue'
import { useRegisterViewRefresh } from '../composables/useViewRefreshRegistry'
import { downloadCsv } from '../utils/csv'

const items = ref<WatchlistItem[]>([])
const quoteMap = ref<Record<string, WatchlistQuote>>({})
const scoreMap = ref<Record<string, WatchlistScore>>({})
const loading = ref(false)
const adding = ref(false)
const addError = ref('')
const activatingId = ref<number | null>(null)
const removingId = ref<number | null>(null)
const scoringSymbol = ref<string | null>(null)
const searchText = ref('')
const marketFilter = ref<'US' | 'HK' | ''>('')
const statusFilter = ref<'active' | 'watching' | ''>('')
const scoreBucket = ref<'high' | 'mid' | 'low' | ''>('')
const hideStaleScores = ref(false)
const sortMode = ref<'default' | 'score_desc' | 'spread_asc' | 'price_desc'>('default')
const selectedIds = ref<number[]>([])
const lastRefreshAt = ref<Date | null>(null)
const bulkDeleteDialog = ref(false)
const newSymbol = ref('')
const newMarket = ref<'US' | 'HK'>('US')
const newAlias = ref('')
let quoteTimer: ReturnType<typeof setInterval> | null = null
// Consecutive quote-fetch failure count. After QUOTE_FAILURE_TOAST_THRESHOLD
// consecutive failures we stop spamming ElMessage.error to avoid drowning the UI.
let quoteFailureStreak = 0
const QUOTE_FAILURE_TOAST_THRESHOLD = 3
const QUOTE_FAILURE_TOAST_COOLDOWN_MS = 60_000
let lastQuoteFailureToastAt = 0

const scoreDrawer = reactive({
  visible: false,
  score: null as WatchlistScore | null,
})

const scoreDetailClass = computed(() => {
  const s = scoreDrawer.score?.score ?? 0
  if (s >= 70) return 'score-high'
  if (s >= 40) return 'score-mid'
  if (s > 0) return 'score-low'
  return 'score-none'
})

const filteredItems = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  const rows = items.value.filter((row) => {
    if (keyword && !`${row.symbol} ${row.alias || ''}`.toLowerCase().includes(keyword)) return false
    if (marketFilter.value && row.market !== marketFilter.value) return false
    if (statusFilter.value === 'active' && !row.is_active) return false
    if (statusFilter.value === 'watching' && row.is_active) return false
    const score = scoreMap.value[row.symbol]
    if (hideStaleScores.value && score?.is_stale) return false
    if (scoreBucket.value) {
      const value = score?.score ?? -1
      if (scoreBucket.value === 'high' && value < 70) return false
      if (scoreBucket.value === 'mid' && (value < 40 || value >= 70)) return false
      if (scoreBucket.value === 'low' && (value < 0 || value >= 40)) return false
    }
    return true
  })
  return rows.sort((a, b) => {
    if (sortMode.value === 'score_desc') return (scoreMap.value[b.symbol]?.score ?? -1) - (scoreMap.value[a.symbol]?.score ?? -1)
    if (sortMode.value === 'spread_asc') return (spreadFor(a) ?? Number.MAX_SAFE_INTEGER) - (spreadFor(b) ?? Number.MAX_SAFE_INTEGER)
    if (sortMode.value === 'price_desc') return (quoteMap.value[b.symbol]?.last_price ?? -1) - (quoteMap.value[a.symbol]?.last_price ?? -1)
    return a.id - b.id
  })
})

const selectedRows = computed(() => filteredItems.value.filter((row) => selectedIds.value.includes(row.id)))

const allFilteredSelected = computed(() => filteredItems.value.length > 0 && filteredItems.value.every((row) => selectedIds.value.includes(row.id)))

const lastRefreshLabel = computed(() => {
  if (!lastRefreshAt.value) return '未刷新'
  return lastRefreshAt.value.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
})

const scoreBucketLabel = computed(() => {
  if (scoreBucket.value === 'high') return '高分 ≥70'
  if (scoreBucket.value === 'mid') return '中分 40-69'
  if (scoreBucket.value === 'low') return '低分 <40'
  return ''
})

const sortModeLabel = computed(() => {
  if (sortMode.value === 'score_desc') return '评分从高到低'
  if (sortMode.value === 'spread_asc') return '价差从小到大'
  if (sortMode.value === 'price_desc') return '最新价从高到低'
  return ''
})

function openScoreDrawer(score: WatchlistScore) {
  scoreDrawer.score = score
  scoreDrawer.visible = true
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function scoreTagType(score: number): 'success' | 'warning' | 'info' | 'danger' {
  if (score >= 70) return 'success'
  if (score >= 40) return 'warning'
  if (score > 0) return 'info'
  return 'danger'
}

function scoreActionLabel(action: string): string {
  switch (action) {
    case 'BUY': return '买入'
    case 'SELL': return '卖出'
    case 'AVOID': return '回避'
    case 'HOLD':
    default:
      return '观望'
  }
}

async function loadItems() {
  loading.value = true
  try {
    items.value = await getWatchlist()
    const ids = new Set(items.value.map((item) => item.id))
    selectedIds.value = selectedIds.value.filter((id) => ids.has(id))
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '加载观察列表失败'))
  } finally {
    loading.value = false
  }
}

async function loadQuotes() {
  if (items.value.length === 0) return
  try {
    const quotes = await getWatchlistQuotes()
    const map: Record<string, WatchlistQuote> = {}
    for (const q of quotes) {
      map[q.symbol] = q
    }
    quoteMap.value = map
    lastRefreshAt.value = new Date()
    quoteFailureStreak = 0
  } catch (e: unknown) {
    quoteFailureStreak += 1
    // Throttle the user-visible toast: only show it for the first 3 consecutive
    // failures, then suppress further toasts for a cooldown window. The streak
    // counter resets on the next successful fetch.
    const now = Date.now()
    const inCooldown = now - lastQuoteFailureToastAt < QUOTE_FAILURE_TOAST_COOLDOWN_MS
    if (quoteFailureStreak <= QUOTE_FAILURE_TOAST_THRESHOLD && !inCooldown) {
      ElMessage.error(resolveErrorMessage(e, '加载行情失败'))
      lastQuoteFailureToastAt = now
    }
    if (quoteFailureStreak === QUOTE_FAILURE_TOAST_THRESHOLD) {
      // One final warning so the user knows the silent mode has kicked in.
      ElMessage.warning('行情连续加载失败，已暂停错误提示，下次成功后将恢复')
    }
  }
}

function clearFilters() {
  searchText.value = ''
  marketFilter.value = ''
  statusFilter.value = ''
  scoreBucket.value = ''
  hideStaleScores.value = false
  sortMode.value = 'default'
}

function toggleSelection(id: number) {
  selectedIds.value = selectedIds.value.includes(id)
    ? selectedIds.value.filter((value) => value !== id)
    : [...selectedIds.value, id]
}

function toggleSelectAll(value: string | number | boolean) {
  if (Boolean(value)) {
    selectedIds.value = Array.from(new Set([...selectedIds.value, ...filteredItems.value.map((row) => row.id)]))
  } else {
    const filtered = new Set(filteredItems.value.map((row) => row.id))
    selectedIds.value = selectedIds.value.filter((id) => !filtered.has(id))
  }
}

async function copySymbol(symbol: string) {
  try {
    await navigator.clipboard.writeText(symbol)
    ElMessage.success(`已复制 ${symbol}`)
  } catch {
    ElMessage.error('复制失败')
  }
}

function rowsToCsv(rows: WatchlistItem[]) {
  return rows.map((row) => {
    const q = quoteMap.value[row.symbol]
    const s = scoreMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      score: s?.score ?? '',
      is_active: row.is_active ? 'yes' : 'no',
    }
  })
}

function exportSelected() {
  downloadCsv('watchlist_selected.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'alias', label: 'alias' },
    { key: 'last_price', label: 'last_price' },
    { key: 'score', label: 'score' },
    { key: 'is_active', label: 'is_active' },
  ], rowsToCsv(selectedRows.value))
  ElMessage.success(`已导出 ${selectedRows.value.length} 个标的`)
}

async function confirmBulkDelete() {
  const ids = selectedRows.value.map((row) => row.id)
  bulkDeleteDialog.value = false
  const results = await Promise.allSettled(ids.map((id) => removeWatchlistItem(id)))
  const failed = results.filter((result) => result.status === 'rejected').length
  selectedIds.value = selectedIds.value.filter((id) => !ids.includes(id))
  try {
    await loadItems()
    await loadQuotes()
  } finally {
    if (failed > 0) {
      ElMessage.error(`批量删除完成：成功 ${ids.length - failed}，失败 ${failed}`)
    } else {
      ElMessage.success(`已删除 ${ids.length} 个标的`)
    }
  }
}

async function refreshNow() {
  await loadQuotes()
  await loadScores()
}

async function handleAdd() {
  if (!newSymbol.value.trim()) return
  adding.value = true
  addError.value = ''
  try {
    await addWatchlistItem({
      symbol: newSymbol.value.trim().toUpperCase(),
      market: newMarket.value,
      alias: newAlias.value.trim(),
    })
    newSymbol.value = ''
    newAlias.value = ''
    await loadItems()
    await loadQuotes()
  } catch (e: unknown) {
    addError.value = resolveErrorMessage(e, '添加失败')
  } finally {
    adding.value = false
  }
}

async function handleRemove(id: number) {
  removingId.value = id
  try {
    await removeWatchlistItem(id)
    await loadItems()
    await loadQuotes()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '删除失败'))
  } finally {
    removingId.value = null
  }
}

async function handleActivate(id: number) {
  activatingId.value = id
  try {
    await activateWatchlistItem(id)
    await loadItems()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '激活失败'))
  } finally {
    activatingId.value = null
  }
}

async function loadScores() {
  try {
    const list = await getWatchlistScores()
    const map: Record<string, WatchlistScore> = {}
    for (const row of list) {
      map[row.symbol] = row
    }
    scoreMap.value = map
  } catch (e: unknown) {
    // Non-fatal: the UI still works without scores. Just log via the toast.
    ElMessage.warning(resolveErrorMessage(e, '加载评分失败'))
  }
}

async function handleScore(symbol: string, market: 'US' | 'HK') {
  scoringSymbol.value = symbol
  try {
    const score = await scoreWatchlistSymbol({ symbol, market, ttl_minutes: 60 })
    scoreMap.value = { ...scoreMap.value, [symbol]: score }
    ElMessage.success(`${symbol} 评分 ${score.score.toFixed(0)}（${scoreActionLabel(score.recommended_action)}）`)
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '评分请求失败'))
  } finally {
    scoringSymbol.value = null
  }
}

/** ask - bid for a row, or null when quotes are missing/invalid. */
function spreadFor(row: WatchlistItem): number | null {
  const q = quoteMap.value[row.symbol]
  if (!q || q.ask == null || q.bid == null) return null
  const spread = q.ask - q.bid
  return Number.isFinite(spread) ? spread : null
}

function exportSnapshot() {
  const rows = items.value.map((row) => {
    const q = quoteMap.value[row.symbol]
    const s = scoreMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      bid: q?.bid ?? '',
      ask: q?.ask ?? '',
      spread: spreadFor(row) ?? '',
      score: s?.score ?? '',
      recommended_action: s ? scoreActionLabel(s.recommended_action) : '',
      confidence: s ? s.confidence : '',
      score_stale: s ? (s.is_stale ? 'yes' : 'no') : '',
      is_active: row.is_active ? 'yes' : 'no',
    }
  })
  downloadCsv('watchlist_snapshot.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'alias', label: 'alias' },
    { key: 'last_price', label: 'last_price' },
    { key: 'bid', label: 'bid' },
    { key: 'ask', label: 'ask' },
    { key: 'spread', label: 'spread' },
    { key: 'score', label: 'score' },
    { key: 'recommended_action', label: 'recommended_action' },
    { key: 'confidence', label: 'confidence' },
    { key: 'score_stale', label: 'score_stale' },
    { key: 'is_active', label: 'is_active' },
  ], rows)
  ElMessage.success('已导出观察列表快照')
}

useRegisterViewRefresh(() => {
  void loadItems()
})

onMounted(() => {
  loadItems().then(() => {
    loadQuotes()
    loadScores()
  })
  quoteTimer = setInterval(loadQuotes, 15000)
})

watch([searchText, marketFilter, statusFilter, scoreBucket, hideStaleScores], () => {
  const visible = new Set(filteredItems.value.map((row) => row.id))
  selectedIds.value = selectedIds.value.filter((id) => visible.has(id))
})

onUnmounted(() => {
  if (quoteTimer) clearInterval(quoteTimer)
})
</script>

<style scoped>
.watchlist-page {
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
}

.page-heading {
  margin-bottom: 16px;
}

.page-heading h3 {
  margin: 0 0 4px;
}

.page-heading p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}

.score-tag {
  cursor: pointer;
}

.watchlist-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.watchlist-toolbar-note {
  color: #909399;
  font-size: 12px;
}

.watchlist-filters,
.watchlist-bulk-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.watchlist-filter-summary {
  margin-bottom: 10px;
  color: #606266;
  font-size: 12px;
}

.bulk-delete-symbols {
  color: #606266;
  font-size: 12px;
  word-break: break-all;
}

.score-detail-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 20px;
}

.score-detail-score {
  font-size: 48px;
  font-weight: 800;
  line-height: 1;
}

.score-high {
  color: #14884f;
}

.score-mid {
  color: #e6a23c;
}

.score-low {
  color: #409eff;
}

.score-none {
  color: #909399;
}

.score-detail-section {
  margin-bottom: 16px;
}

.score-detail-label {
  margin-bottom: 6px;
  color: #909399;
  font-size: 12px;
}

.score-detail-rationale {
  margin: 0;
  color: #4b5563;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
