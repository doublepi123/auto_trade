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
      <el-table :data="items" v-loading="loading" style="width: 100%">
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
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success">交易中</el-tag>
            <el-tag v-else type="info">观察中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
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
      <div v-if="items.length === 0 && !loading" style="text-align: center; color: #909399; padding: 32px">
        暂无观察标的，请添加股票代码
      </div>
    </el-card>

    <el-drawer
      v-model="scoreDrawer.visible"
      :title="`${scoreDrawer.score?.symbol || ''} LLM 评分详情`"
      size="380px"
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
import { computed, ref, onMounted, onUnmounted, reactive } from 'vue'
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

const items = ref<WatchlistItem[]>([])
const quoteMap = ref<Record<string, WatchlistQuote>>({})
const scoreMap = ref<Record<string, WatchlistScore>>({})
const loading = ref(false)
const adding = ref(false)
const addError = ref('')
const activatingId = ref<number | null>(null)
const removingId = ref<number | null>(null)
const scoringSymbol = ref<string | null>(null)
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

onMounted(() => {
  loadItems().then(() => {
    loadQuotes()
    loadScores()
  })
  quoteTimer = setInterval(loadQuotes, 15000)
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
