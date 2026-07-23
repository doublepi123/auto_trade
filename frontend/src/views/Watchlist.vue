<template>
  <div class="watchlist-page">
    <div class="page-heading">
      <h3>观察列表</h3>
      <p>多标的行情观察（仅观察，不自动下单）</p>
    </div>

    <el-card class="universe-panel" data-testid="universe-panel">
      <template #header>
        <div class="universe-header">
          <div class="universe-heading">
            <div class="universe-title">
              <strong>动态候选池</strong>
              <el-tag type="info" size="small" effect="plain">动态筛选</el-tag>
              <el-tag type="warning" size="small" effect="plain">只读观察</el-tag>
            </div>
            <p>每日按指数成分、流动性与交易成本筛选；入选不等于切换实盘，也不会自动下单。</p>
          </div>
          <el-button
            type="primary"
            plain
            :icon="Refresh"
            :loading="universeRefreshing"
            data-testid="universe-refresh"
            @click="handleUniverseRefresh"
          >
            刷新候选池
          </el-button>
        </div>
      </template>

      <el-alert
        v-if="universeError"
        :title="universeError"
        type="error"
        :closable="false"
        show-icon
        class="universe-alert"
        data-testid="universe-error"
      />

      <div v-loading="universeLoading && !universeRun" class="universe-content">
        <template v-if="universeRun">
          <div class="universe-summary" data-testid="universe-summary">
            <div class="universe-summary-item">
              <span>数据日期</span>
              <strong data-testid="universe-as-of">{{ universeRun.as_of_date }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>运行状态</span>
              <el-tag :type="universeStatusTagType(universeRun.status)" size="small">
                {{ universeStatusLabel(universeRun.status) }}
              </el-tag>
            </div>
            <div class="universe-summary-item">
              <span>数据覆盖</span>
              <strong data-testid="universe-coverage">{{ formatCoverage(universeRun.coverage_ratio) }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>候选入选</span>
              <strong>{{ universeRun.selected_count }}/{{ universeRun.evaluable_count }}</strong>
            </div>
            <div class="universe-summary-item">
              <span>候选目录</span>
              <strong>{{ universeCatalog.length || universeRun.candidate_count }}</strong>
            </div>
            <div class="universe-summary-item universe-version">
              <span>算法版本</span>
              <strong :title="universeRun.algorithm_version">{{ universeRun.algorithm_version }}</strong>
            </div>
          </div>

          <el-alert
            v-if="universeRun.error"
            :title="universeRun.error"
            type="warning"
            :closable="false"
            show-icon
            class="universe-alert"
          />

          <div class="universe-table-view">
            <el-table
              :data="universeRows"
              max-height="440"
              style="width: 100%"
              data-testid="universe-table"
            >
              <el-table-column label="候选" min-width="190">
                <template #default="{ row }">
                  <div class="universe-symbol">
                    <strong>{{ row.symbol }}</strong>
                    <small>{{ row.alias || row.sector || '-' }}</small>
                    <div class="universe-memberships">
                      <el-tag
                        v-for="membership in row.memberships"
                        :key="membership"
                        size="small"
                        effect="plain"
                      >
                        {{ membershipLabel(membership) }}
                      </el-tag>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="126">
                <template #default="{ row }">
                  <div class="universe-state-tags">
                    <el-tag v-if="row.is_trading_target" type="danger" size="small">
                      当前实盘
                    </el-tag>
                    <el-tag :type="row.selected ? 'success' : 'info'" size="small" effect="plain">
                      {{ row.selected ? '候选入选' : '未入选' }}
                    </el-tag>
                    <el-tag v-if="row.shadow_enabled" type="warning" size="small" effect="plain">
                      Shadow 已启用
                    </el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="排名/分数" width="96" align="right">
                <template #default="{ row }">
                  <strong>{{ row.rank ? `#${row.rank}` : '-' }}</strong>
                  <small class="universe-score">{{ formatScore(row.score) }}</small>
                </template>
              </el-table-column>
              <el-table-column label="日均流动性" width="118" align="right">
                <template #default="{ row }">{{ formatDollarVolume(row.metrics.avg_dollar_volume) }}</template>
              </el-table-column>
              <el-table-column label="T-1 成本" width="92" align="right">
                <template #default="{ row }">{{ formatBps(row.metrics.relative_spread_bps) }}</template>
              </el-table-column>
              <el-table-column label="20日波动" width="88" align="right">
                <template #default="{ row }">{{ formatVolatility(row.metrics.realized_vol_20d) }}</template>
              </el-table-column>
              <el-table-column label="14日 ATR" width="86" align="right">
                <template #default="{ row }">{{ formatAtr(row.metrics.atr_pct_14d) }}</template>
              </el-table-column>
              <el-table-column label="筛选结论" min-width="190">
                <template #default="{ row }">
                  <div v-if="row.exclusion_reasons.length" class="universe-reasons">
                    <el-tag
                      v-for="reason in row.exclusion_reasons"
                      :key="reason"
                      type="info"
                      size="small"
                      effect="plain"
                    >
                      {{ exclusionReasonLabel(reason) }}
                    </el-tag>
                  </div>
                  <span v-else class="universe-pass">通过硬性门槛</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="universe-mobile-list" data-testid="universe-mobile-list">
            <article v-for="row in universeRows" :key="row.symbol" class="universe-mobile-row">
              <div class="universe-mobile-heading">
                <div>
                  <strong>{{ row.symbol }}</strong>
                  <small>{{ row.alias || row.sector || '-' }}</small>
                </div>
                <div class="universe-state-tags">
                  <el-tag v-if="row.is_trading_target" type="danger" size="small">当前实盘</el-tag>
                  <el-tag :type="row.selected ? 'success' : 'info'" size="small" effect="plain">
                    {{ row.selected ? `候选 #${row.rank ?? '-'}` : '未入选' }}
                  </el-tag>
                  <el-tag v-if="row.shadow_enabled" type="warning" size="small" effect="plain">
                    Shadow 已启用
                  </el-tag>
                </div>
              </div>
              <div class="universe-mobile-memberships">
                <el-tag
                  v-for="membership in row.memberships"
                  :key="membership"
                  size="small"
                  effect="plain"
                >
                  {{ membershipLabel(membership) }}
                </el-tag>
              </div>
              <div class="universe-mobile-metrics">
                <div><span>分数</span><strong>{{ formatScore(row.score) }}</strong></div>
                <div><span>流动性</span><strong>{{ formatDollarVolume(row.metrics.avg_dollar_volume) }}</strong></div>
                <div><span>T-1 成本</span><strong>{{ formatBps(row.metrics.relative_spread_bps) }}</strong></div>
                <div><span>波动</span><strong>{{ formatVolatility(row.metrics.realized_vol_20d) }}</strong></div>
                <div><span>ATR</span><strong>{{ formatAtr(row.metrics.atr_pct_14d) }}</strong></div>
              </div>
              <div class="universe-mobile-reason">
                {{ row.exclusion_reasons.length
                  ? row.exclusion_reasons.map(exclusionReasonLabel).join(' · ')
                  : '通过硬性门槛' }}
              </div>
            </article>
          </div>

          <div class="universe-footer">
            <span>
              完成于 {{ formatDateTime(universeRun.completed_at || universeRun.created_at) }}
              · 来源 {{ universeRun.source_version }}
            </span>
            <span>候选池是观察信号；当前实盘标的仍由下方“交易中”状态明确标识。</span>
          </div>
        </template>

        <DataState
          v-else-if="!universeLoading"
          empty
          :empty-text="universeCatalog.length
            ? `候选目录已加载 ${universeCatalog.length} 个标的，尚无筛选记录`
            : '尚无动态候选池记录'"
        />
      </div>
    </el-card>

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
        <el-button
          size="small"
          plain
          :disabled="quantRanking || scoringSymbol !== null"
          data-testid="watchlist-refresh-now"
          @click="refreshNow"
        >
          手动刷新
        </el-button>
        <el-button
          size="small"
          type="primary"
          plain
          :icon="DataAnalysis"
          :loading="quantRanking"
          :disabled="items.length === 0"
          data-testid="watchlist-quant-rank"
          @click="handleQuantRank"
        >
          量化评分
        </el-button>
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
          <el-option label="量化优选" value="high" />
          <el-option label="量化观察" value="mid" />
          <el-option label="量化回避" value="low" />
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
      <div class="watchlist-table-scroll">
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
          <template #default="{ row }">
            <div>{{ row.alias || '-' }}</div>
            <small class="watchlist-source">{{ watchlistSourceLabel(row.source) }}</small>
          </template>
        </el-table-column>
        <el-table-column
          label="候选评估"
          width="270"
          sortable
          :sort-by="(row: WatchlistItem) => scoreMap[row.symbol]?.score ?? -1"
        >
          <template #default="{ row }">
            <div class="score-stack">
              <div
                v-if="scoreMap[row.symbol]"
                class="score-channel"
                data-testid="watchlist-quant-score"
              >
                <span class="score-channel-label">量化主分</span>
                <el-tag
                  :type="scoreTagType(scoreMap[row.symbol])"
                  size="small"
                  class="score-tag"
                  data-testid="watchlist-score-tag"
                  @click="openScoreDrawer(scoreMap[row.symbol])"
                >
                  {{ scoreMap[row.symbol].score.toFixed(0) }}
                </el-tag>
                <span class="score-outcome">{{ scoreOutcomeLabel(scoreMap[row.symbol]) }}</span>
                <span class="score-source">{{ scoreSourceLabel(scoreMap[row.symbol].source) }}</span>
                <el-tag
                  v-if="isScoreStale(scoreMap[row.symbol])"
                  type="warning"
                  size="small"
                  data-testid="watchlist-stale-badge"
                >
                  已过期
                </el-tag>
              </div>
              <div v-else class="score-channel score-channel-empty">
                <span class="score-channel-label">量化主分</span>
                <span>未评分</span>
              </div>
              <div
                v-if="reviewMap[row.symbol]"
                class="score-channel"
                data-testid="watchlist-review-score"
              >
                <span class="score-channel-label">AI 复核</span>
                <el-tag
                  :type="scoreTagType(reviewMap[row.symbol])"
                  size="small"
                  class="score-tag"
                  data-testid="watchlist-score-tag"
                  @click="openScoreDrawer(reviewMap[row.symbol])"
                >
                  {{ reviewMap[row.symbol].score.toFixed(0) }}
                </el-tag>
                <span class="score-outcome">{{ scoreOutcomeLabel(reviewMap[row.symbol]) }}</span>
                <span class="score-source">{{ scoreSourceLabel(reviewMap[row.symbol].source) }}</span>
                <el-tag
                  v-if="isScoreStale(reviewMap[row.symbol])"
                  type="warning"
                  size="small"
                  data-testid="watchlist-stale-badge"
                >
                  已过期
                </el-tag>
              </div>
              <div v-else class="score-channel score-channel-empty">
                <span class="score-channel-label">AI 复核</span>
                <span>未复核</span>
              </div>
            </div>
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
            <el-tag v-if="row.is_trading_target" type="success">交易中</el-tag>
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
              v-if="!row.is_trading_target"
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
              :disabled="scoringSymbol !== null && scoringSymbol !== row.symbol"
              :aria-label="`对 ${row.symbol} 进行 AI 复核`"
              @click="handleScore(row.symbol, row.market)"
            >
              AI 复核
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
      </div>
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
      :title="`${scoreDrawer.score?.symbol || ''} ${scoreDrawerChannelLabel}详情`"
      size="380px"
      destroy-on-close
      data-testid="watchlist-score-drawer"
    >
      <template v-if="scoreDrawer.score">
        <div class="score-detail-header">
          <div class="score-detail-score" :class="scoreDetailClass">{{ scoreDrawer.score.score.toFixed(0) }}</div>
          <el-tag :type="scoreTagType(scoreDrawer.score)" size="small">{{ scoreOutcomeLabel(scoreDrawer.score) }}</el-tag>
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
          <el-tag :type="scoreSourceTagType(scoreDrawer.score.source)" size="small">
            {{ scoreSourceLabel(scoreDrawer.score.source) }}
          </el-tag>
          <el-tag v-if="isScoreStale(scoreDrawer.score)" type="warning" size="small">已过期</el-tag>
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
import { DataAnalysis, Refresh } from '@element-plus/icons-vue'
import { isAxiosError } from 'axios'
import type {
  UniverseCatalogItem,
  UniverseSelectionItem,
  UniverseSelectionRunResponse,
  WatchlistItem,
  WatchlistQuote,
} from '../types'
import {
  getWatchlist,
  addWatchlistItem,
  removeWatchlistItem,
  activateWatchlistItem,
  getWatchlistQuotes,
  getWatchlistScores,
  rankWatchlistQuant,
  scoreWatchlistSymbol,
  type WatchlistScore,
} from '../api/watchlist'
import {
  getLatestUniverseSelection,
  getUniverseCatalog,
  refreshUniverseSelection,
} from '../api/universe'
import { formatCurrency } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'
import DataState from '../components/DataState.vue'
import { useRegisterViewRefresh } from '../composables/useViewRefreshRegistry'
import { downloadCsv } from '../utils/csv'

const items = ref<WatchlistItem[]>([])
const quoteMap = ref<Record<string, WatchlistQuote>>({})
const scoreMap = ref<Record<string, WatchlistScore>>({})
const reviewMap = ref<Record<string, WatchlistScore>>({})
const loading = ref(false)
const adding = ref(false)
const quantRanking = ref(false)
const universeCatalog = ref<UniverseCatalogItem[]>([])
const universeRun = ref<UniverseSelectionRunResponse | null>(null)
const universeLoading = ref(false)
const universeRefreshing = ref(false)
const universeError = ref('')
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
const scoreClockMs = ref(Date.now())
let quoteTimer: ReturnType<typeof setInterval> | null = null
let scoreExpiryTimer: ReturnType<typeof setInterval> | null = null
// Consecutive quote-fetch failure count. After QUOTE_FAILURE_TOAST_THRESHOLD
// consecutive failures we stop spamming ElMessage.error to avoid drowning the UI.
let quoteFailureStreak = 0
const QUOTE_FAILURE_TOAST_THRESHOLD = 3
const QUOTE_FAILURE_TOAST_COOLDOWN_MS = 60_000
let lastQuoteFailureToastAt = 0
let universeRequestGeneration = 0
let quantScoreGeneration = 0
let reviewScoreGeneration = 0

const scoreDrawer = reactive({
  visible: false,
  score: null as WatchlistScore | null,
})

const scoreDetailClass = computed(() => {
  return scoreDrawer.score ? scoreVisualClass(scoreDrawer.score) : 'score-none'
})

const scoreDrawerChannelLabel = computed(() => (
  scoreDrawer.score && isQuantScore(scoreDrawer.score)
    ? '量化主分'
    : 'AI 复核'
))

const universeRows = computed<UniverseSelectionItem[]>(() => {
  if (!universeRun.value) return []
  return [...universeRun.value.items].sort((left, right) => {
    if (left.selected !== right.selected) return left.selected ? -1 : 1
    if (left.rank !== null && right.rank !== null) return left.rank - right.rank
    if (left.rank !== null) return -1
    if (right.rank !== null) return 1
    if (left.score !== right.score) return right.score - left.score
    return left.symbol.localeCompare(right.symbol)
  })
})

const filteredItems = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  const rows = items.value.filter((row) => {
    if (keyword && !`${row.symbol} ${row.alias || ''}`.toLowerCase().includes(keyword)) return false
    if (marketFilter.value && row.market !== marketFilter.value) return false
    if (statusFilter.value === 'active' && !row.is_trading_target) return false
    if (statusFilter.value === 'watching' && row.is_trading_target) return false
    const score = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    if (
      hideStaleScores.value
      && (
        (score !== undefined && isScoreStale(score))
        || (score === undefined && review !== undefined && isScoreStale(review))
      )
    ) return false
    if (scoreBucket.value && quantScoreBucket(score) !== scoreBucket.value) return false
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
  if (scoreBucket.value === 'high') return '量化优选'
  if (scoreBucket.value === 'mid') return '量化观察'
  if (scoreBucket.value === 'low') return '量化回避'
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

function universeStatusLabel(status: string): string {
  switch (status.toUpperCase()) {
    case 'COMPLETE':
    case 'SUCCEEDED':
    case 'COMPLETED':
      return '已完成'
    case 'DEGRADED':
      return '数据不足'
    case 'RUNNING':
      return '运行中'
    case 'FAILED':
      return '失败'
    default:
      return status
  }
}

function universeStatusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status.toUpperCase()) {
    case 'COMPLETE':
    case 'SUCCEEDED':
    case 'COMPLETED':
      return 'success'
    case 'DEGRADED':
    case 'RUNNING':
      return 'warning'
    case 'FAILED':
      return 'danger'
    default:
      return 'info'
  }
}

function membershipLabel(membership: string): string {
  if (membership === 'NASDAQ_100') return '纳指 100'
  if (membership === 'DJIA') return '道指'
  return membership
}

const exclusionReasonLabels: Record<string, string> = {
  DATA_INSUFFICIENT_DAILY_BARS: '日线数据不足',
  DATA_NON_FINITE_DAILY_BAR: '日线含无效值',
  DATA_INVALID_DAILY_BAR: '日线结构异常',
  DATA_INVALID_QUOTE: '买卖盘无效',
  DATA_FETCH_FAILED: '行情获取失败',
  DATA_QUOTE_MISSING: '报价缺失',
  DATA_NO_COMPLETED_DAILY_BAR: '无完整日线',
  DATA_STALE_SESSION_DATE: '交易日数据陈旧',
  PRICE_BELOW_MINIMUM: '股价低于门槛',
  DOLLAR_VOLUME_BELOW_MINIMUM: '流动性不足',
  SPREAD_ABOVE_MAXIMUM: '成本代理过高',
  REALIZED_VOL_OUTSIDE_RANGE: '波动率超出区间',
  ATR_OUTSIDE_RANGE: 'ATR 超出区间',
  SECTOR_CAP: '行业名额已满',
  BELOW_SELECTION_CUTOFF: '综合排序未入围',
}

function exclusionReasonLabel(reason: string): string {
  return exclusionReasonLabels[reason] ?? reason.replace(/_/g, ' ')
}

function formatCoverage(value: number): string {
  if (!Number.isFinite(value)) return '-'
  return `${(Math.min(1, Math.max(0, value)) * 100).toFixed(1)}%`
}

function formatScore(value: number | null | undefined): string {
  return value !== null && value !== undefined && Number.isFinite(value)
    ? value.toFixed(1)
    : '-'
}

function formatDollarVolume(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '-'
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(2)}B`
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`
  return `$${value.toFixed(0)}`
}

function formatBps(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${value.toFixed(1)} bp` : '-'
}

function formatVolatility(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '-'
}

function formatAtr(value: number | null): string {
  return value !== null && Number.isFinite(value) ? `${value.toFixed(2)}%` : '-'
}

function isQuantScore(score: WatchlistScore): boolean {
  return score.source.startsWith('quant_')
}

function isScoreStale(score: WatchlistScore): boolean {
  if (score.is_stale) return true
  const expiresAtMs = Date.parse(score.expires_at)
  return Number.isFinite(expiresAtMs) && expiresAtMs <= scoreClockMs.value
}

function quantScoreBucket(
  score: WatchlistScore | undefined,
): 'high' | 'mid' | 'low' | null {
  if (!score || !isQuantScore(score)) return null
  switch (score.recommended_action) {
    case 'CANDIDATE': return 'high'
    case 'WATCH': return 'mid'
    case 'AVOID': return 'low'
    default: return null
  }
}

function scoreTagType(
  score: WatchlistScore,
): 'success' | 'warning' | 'info' | 'danger' {
  if (score.source.startsWith('fallback')) return 'info'
  if (isQuantScore(score)) {
    if (score.recommended_action === 'CANDIDATE') return 'success'
    if (score.recommended_action === 'WATCH') return 'warning'
    return score.source === 'quant_error' ? 'danger' : 'info'
  }
  if (score.recommended_action === 'BUY') return 'success'
  if (score.recommended_action === 'SELL') return 'warning'
  if (score.recommended_action === 'AVOID') return 'danger'
  return 'info'
}

function scoreVisualClass(score: WatchlistScore): string {
  const type = scoreTagType(score)
  if (type === 'success') return 'score-high'
  if (type === 'warning') return 'score-mid'
  if (type === 'danger') return 'score-low'
  return 'score-none'
}

function scoreActionLabel(action: string): string {
  switch (action) {
    case 'CANDIDATE': return '优选'
    case 'WATCH': return '观察'
    case 'BUY': return '买入'
    case 'SELL': return '卖出'
    case 'AVOID': return '回避'
    case 'HOLD':
    default:
      return '观望'
  }
}

function scoreOutcomeLabel(score: WatchlistScore): string {
  if (score.source.startsWith('fallback')) return '中性兜底'
  if (score.source === 'quant_error') return '数据异常'
  return scoreActionLabel(score.recommended_action)
}

function scoreSourceLabel(source: string): string {
  if (source === 'quant_v1') return '量化 v1'
  if (source === 'quant_error') return '数据异常'
  if (source === 'llm') return 'AI 复核'
  if (source.startsWith('fallback')) return 'AI 降级结果'
  return source
}

function scoreSourceTagType(
  source: string,
): 'success' | 'warning' | 'info' {
  if (source === 'quant_error') return 'warning'
  if (source.startsWith('fallback')) return 'info'
  return 'success'
}

function watchlistSourceLabel(source: string): string {
  return source === 'universe' ? '指数候选池' : '手动添加'
}

async function loadUniverse() {
  if (universeRefreshing.value) return
  const generation = ++universeRequestGeneration
  universeLoading.value = true
  universeError.value = ''
  const errors: string[] = []
  try {
    const [catalogResult, latestResult] = await Promise.allSettled([
      getUniverseCatalog(),
      getLatestUniverseSelection(),
    ])
    if (generation !== universeRequestGeneration) return
    if (catalogResult.status === 'fulfilled') {
      universeCatalog.value = catalogResult.value
    } else {
      errors.push(resolveErrorMessage(catalogResult.reason, '加载候选目录失败'))
    }

    if (latestResult.status === 'fulfilled') {
      universeRun.value = latestResult.value
    } else if (isAxiosError(latestResult.reason) && latestResult.reason.response?.status === 404) {
      universeRun.value = null
    } else {
      errors.push(resolveErrorMessage(latestResult.reason, '加载候选池结果失败'))
    }
    universeError.value = errors.join('；')
  } finally {
    if (generation === universeRequestGeneration) {
      universeLoading.value = false
    }
  }
}

async function handleUniverseRefresh() {
  const generation = ++universeRequestGeneration
  universeRefreshing.value = true
  universeLoading.value = false
  universeError.value = ''
  try {
    const response = await refreshUniverseSelection()
    if (generation !== universeRequestGeneration) return
    universeRun.value = response.run
    if (response.applied) {
      await loadItems()
      await loadQuotes()
    }
    const summary = `候选池已刷新：候选入选 ${response.run.selected_count} 个，覆盖率 ${formatCoverage(response.run.coverage_ratio)}`
    if (response.run.status.toUpperCase() !== 'COMPLETE') {
      ElMessage.warning(`${summary}；${response.run.error || response.reason || '数据覆盖不足'}`)
    } else if (response.shadow_failed_symbols.length > 0) {
      ElMessage.warning(
        `${summary}；部分 Shadow 同步失败：${response.shadow_failed_symbols.join('、')}`,
      )
    } else {
      ElMessage.success(summary)
    }
  } catch (e: unknown) {
    if (generation !== universeRequestGeneration) return
    universeError.value = resolveErrorMessage(e, '刷新候选池失败')
    ElMessage.error(universeError.value)
  } finally {
    if (generation === universeRequestGeneration) {
      universeRefreshing.value = false
    }
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
    const quant = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      quant_score: quant?.score ?? '',
      quant_action: quant ? scoreOutcomeLabel(quant) : '',
      quant_source: quant ? scoreSourceLabel(quant.source) : '',
      review_score: review?.score ?? '',
      review_action: review ? scoreOutcomeLabel(review) : '',
      review_source: review ? scoreSourceLabel(review.source) : '',
      is_trading_target: row.is_trading_target ? 'yes' : 'no',
    }
  })
}

function exportSelected() {
  downloadCsv('watchlist_selected.csv', [
    { key: 'symbol', label: 'symbol' },
    { key: 'market', label: 'market' },
    { key: 'alias', label: 'alias' },
    { key: 'last_price', label: 'last_price' },
    { key: 'quant_score', label: 'quant_score' },
    { key: 'quant_action', label: 'quant_action' },
    { key: 'quant_source', label: 'quant_source' },
    { key: 'review_score', label: 'review_score' },
    { key: 'review_action', label: 'review_action' },
    { key: 'review_source', label: 'review_source' },
    { key: 'is_trading_target', label: 'is_trading_target' },
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
  if (!quantRanking.value && scoringSymbol.value === null) {
    await loadScores()
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
  if (quantRanking.value || scoringSymbol.value !== null) return
  const quantGeneration = ++quantScoreGeneration
  const reviewGeneration = ++reviewScoreGeneration
  try {
    const response = await getWatchlistScores()
    if (quantGeneration === quantScoreGeneration) {
      const quantMap: Record<string, WatchlistScore> = {}
      for (const row of response.scores) {
        if (isQuantScore(row)) quantMap[row.symbol] = row
      }
      scoreMap.value = quantMap
    }
    if (reviewGeneration === reviewScoreGeneration) {
      const latestReviews: Record<string, WatchlistScore> = {}
      for (const row of response.reviews) {
        if (!isQuantScore(row)) latestReviews[row.symbol] = row
      }
      reviewMap.value = latestReviews
    }
  } catch (e: unknown) {
    if (
      quantGeneration === quantScoreGeneration
      || reviewGeneration === reviewScoreGeneration
    ) {
      ElMessage.warning(resolveErrorMessage(e, '加载候选评估失败'))
    }
  }
}

async function handleQuantRank() {
  const generation = ++quantScoreGeneration
  quantRanking.value = true
  try {
    const scores = await rankWatchlistQuant(360)
    if (generation !== quantScoreGeneration) return
    const map: Record<string, WatchlistScore> = {}
    for (const score of scores) {
      if (isQuantScore(score)) map[score.symbol] = score
    }
    scoreMap.value = map
    const preferred = scores.filter(
      (score) => score.recommended_action === 'CANDIDATE',
    ).length
    ElMessage.success(
      `量化评分完成：${scores.length} 个标的，优选 ${preferred} 个`,
    )
  } catch (e: unknown) {
    if (generation === quantScoreGeneration) {
      ElMessage.error(resolveErrorMessage(e, '量化评分失败'))
    }
  } finally {
    if (generation === quantScoreGeneration) {
      quantRanking.value = false
    }
  }
}

async function handleScore(symbol: string, market: 'US' | 'HK') {
  if (scoringSymbol.value !== null) return
  const generation = ++reviewScoreGeneration
  scoringSymbol.value = symbol
  try {
    const score = await scoreWatchlistSymbol({ symbol, market, ttl_minutes: 60 })
    if (generation !== reviewScoreGeneration) return
    reviewMap.value = { ...reviewMap.value, [symbol]: score }
    ElMessage.success(
      `${symbol} AI 复核 ${score.score.toFixed(0)}（${scoreOutcomeLabel(score)}）`,
    )
  } catch (e: unknown) {
    if (generation === reviewScoreGeneration) {
      ElMessage.error(resolveErrorMessage(e, 'AI 复核请求失败'))
    }
  } finally {
    if (generation === reviewScoreGeneration) {
      scoringSymbol.value = null
    }
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
    const quant = scoreMap.value[row.symbol]
    const review = reviewMap.value[row.symbol]
    return {
      symbol: row.symbol,
      market: row.market,
      alias: row.alias || '',
      last_price: q?.last_price ?? '',
      bid: q?.bid ?? '',
      ask: q?.ask ?? '',
      spread: spreadFor(row) ?? '',
      quant_score: quant?.score ?? '',
      quant_action: quant ? scoreOutcomeLabel(quant) : '',
      quant_source: quant ? scoreSourceLabel(quant.source) : '',
      quant_confidence: quant?.confidence ?? '',
      quant_stale: quant ? (isScoreStale(quant) ? 'yes' : 'no') : '',
      review_score: review?.score ?? '',
      review_action: review ? scoreOutcomeLabel(review) : '',
      review_source: review ? scoreSourceLabel(review.source) : '',
      review_confidence: review?.confidence ?? '',
      review_stale: review ? (isScoreStale(review) ? 'yes' : 'no') : '',
      is_trading_target: row.is_trading_target ? 'yes' : 'no',
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
    { key: 'quant_score', label: 'quant_score' },
    { key: 'quant_action', label: 'quant_action' },
    { key: 'quant_source', label: 'quant_source' },
    { key: 'quant_confidence', label: 'quant_confidence' },
    { key: 'quant_stale', label: 'quant_stale' },
    { key: 'review_score', label: 'review_score' },
    { key: 'review_action', label: 'review_action' },
    { key: 'review_source', label: 'review_source' },
    { key: 'review_confidence', label: 'review_confidence' },
    { key: 'review_stale', label: 'review_stale' },
    { key: 'is_trading_target', label: 'is_trading_target' },
  ], rows)
  ElMessage.success('已导出观察列表快照')
}

useRegisterViewRefresh(() => {
  void Promise.all([loadItems(), loadUniverse()])
})

onMounted(() => {
  scoreClockMs.value = Date.now()
  void loadUniverse()
  loadItems().then(() => {
    loadQuotes()
    loadScores()
  })
  quoteTimer = setInterval(loadQuotes, 15000)
  scoreExpiryTimer = setInterval(() => {
    scoreClockMs.value = Date.now()
  }, 60_000)
})

watch([searchText, marketFilter, statusFilter, scoreBucket, hideStaleScores], () => {
  const visible = new Set(filteredItems.value.map((row) => row.id))
  selectedIds.value = selectedIds.value.filter((id) => visible.has(id))
})

onUnmounted(() => {
  universeRequestGeneration += 1
  quantScoreGeneration += 1
  reviewScoreGeneration += 1
  if (quoteTimer) clearInterval(quoteTimer)
  if (scoreExpiryTimer) clearInterval(scoreExpiryTimer)
})
</script>

<style scoped>
.watchlist-page {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
  overflow-x: hidden;
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

.universe-panel {
  margin-bottom: 20px;
}

.universe-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.universe-heading {
  min-width: 0;
}

.universe-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.universe-heading p {
  margin: 5px 0 0;
  color: #7a8089;
  font-size: 12px;
  line-height: 1.5;
}

.universe-content {
  min-height: 72px;
}

.universe-alert {
  margin-bottom: 12px;
}

.universe-summary {
  display: grid;
  grid-template-columns: repeat(6, minmax(86px, 1fr));
  gap: 1px;
  margin-bottom: 14px;
  overflow: hidden;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #ebeef5;
}

.universe-summary-item {
  display: flex;
  min-width: 0;
  min-height: 58px;
  padding: 9px 10px;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  background: var(--el-bg-color);
}

.universe-summary-item span {
  color: #909399;
  font-size: 11px;
}

.universe-summary-item strong {
  overflow: hidden;
  color: #303133;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.universe-symbol {
  display: flex;
  min-width: 0;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
}

.universe-symbol small,
.universe-score {
  display: block;
  color: #909399;
  font-size: 11px;
}

.universe-memberships,
.universe-state-tags,
.universe-reasons {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.universe-state-tags {
  align-items: flex-start;
  flex-direction: column;
}

.universe-pass {
  color: #14884f;
  font-size: 12px;
}

.universe-table-view {
  max-width: 100%;
  overflow: hidden;
}

.universe-mobile-list {
  display: none;
}

.universe-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  color: #909399;
  font-size: 11px;
  line-height: 1.5;
}

.score-tag {
  cursor: pointer;
}

.score-stack {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
}

.score-channel {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.score-channel-label {
  width: 52px;
  flex: 0 0 52px;
  color: #606266;
  font-size: 11px;
}

.score-channel-empty,
.score-source,
.score-outcome {
  color: #909399;
  font-size: 10px;
}

.score-source {
  padding-left: 5px;
  border-left: 1px solid #dcdfe6;
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

.watchlist-source {
  color: #909399;
  font-size: 10px;
}

.watchlist-table-scroll {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
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

@media (max-width: 720px) {
  .watchlist-page {
    padding: 10px;
  }

  .universe-panel :deep(.el-card__header),
  .universe-panel :deep(.el-card__body) {
    padding: 12px;
  }

  .universe-header {
    align-items: stretch;
    flex-direction: column;
  }

  .universe-header .el-button {
    width: 100%;
    margin: 0;
  }

  .universe-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .universe-table-view {
    display: none;
  }

  .universe-mobile-list {
    display: block;
    max-height: 520px;
    overflow-y: auto;
    border-top: 1px solid #ebeef5;
    border-bottom: 1px solid #ebeef5;
  }

  .universe-mobile-row {
    min-width: 0;
    padding: 12px 0;
    border-bottom: 1px solid #ebeef5;
  }

  .universe-mobile-row:last-child {
    border-bottom: 0;
  }

  .universe-mobile-heading {
    display: flex;
    min-width: 0;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
  }

  .universe-mobile-heading > div:first-child {
    display: flex;
    min-width: 0;
    flex-direction: column;
  }

  .universe-mobile-heading small {
    overflow: hidden;
    margin-top: 2px;
    color: #909399;
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .universe-mobile-heading .universe-state-tags {
    flex-shrink: 0;
    align-items: flex-end;
  }

  .universe-mobile-memberships {
    display: flex;
    gap: 4px;
    margin-top: 7px;
    flex-wrap: wrap;
  }

  .universe-mobile-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin-top: 10px;
  }

  .universe-mobile-metrics div {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .universe-mobile-metrics span {
    color: #909399;
    font-size: 10px;
  }

  .universe-mobile-metrics strong {
    overflow: hidden;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .universe-mobile-reason {
    margin-top: 9px;
    color: #606266;
    font-size: 11px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .universe-footer {
    flex-direction: column;
    gap: 2px;
  }
}
</style>
