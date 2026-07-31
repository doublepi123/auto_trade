<template>
  <div class="experiments-page" data-testid="experiments-page">
    <h2>策略实验</h2>

    <el-card header="创建实验" data-testid="create-experiment-card">
      <el-form label-width="120px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="实验名称">
              <el-input v-model="name" placeholder="如 AAPL May grid" data-testid="exp-name" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="标的代码">
              <el-input v-model="symbol" placeholder="如 AAPL.US" data-testid="exp-symbol" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="买入价 (buy_low)">
              <el-input-number v-model="buyLow" :precision="2" :step="1" data-testid="exp-buy-low" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="卖出价 (sell_high)">
              <el-input-number v-model="sellHigh" :precision="2" :step="1" data-testid="exp-sell-high" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="数量 (quantity)">
              <el-input-number v-model="quantity" :precision="0" :step="1" :min="1" data-testid="exp-quantity" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="费率 (fee_rate)">
              <el-input-number v-model="feeRate" :precision="4" :step="0.0001" :min="0" data-testid="exp-fee-rate" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="滑点% (slippage_pct)">
          <el-input-number v-model="slippagePct" :precision="3" :step="0.1" :min="0" data-testid="exp-slippage" />
        </el-form-item>

        <el-divider content-position="left">执行与退出（基准参数）</el-divider>

        <el-alert
          class="execution-fidelity-notice"
          title="OHLC K 线近似，非实盘等价：时段按 K 线观测时间判断，日损/时间/EOD 强制退出按收盘价近似，止损与目标价按 OHLC 触及近似；新鲜阈值穿越仅作保守近似，且不包含历史 BBO、部分成交、拒单或动态全买力回放。"
          type="info"
          show-icon
          :closable="false"
          data-testid="exp-fidelity-notice"
        />

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="市场 (market)">
              <el-select v-model="market" data-testid="exp-market">
                <el-option label="美股 (US)" value="US" />
                <el-option label="港股 (HK)" value="HK" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="交易时段">
              <el-select v-model="tradingSessionMode" data-testid="exp-session-mode">
                <el-option label="任意时段（兼容模式）" value="ANY" />
                <el-option label="仅常规交易时段" value="RTH_ONLY" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="止损 %">
              <el-input-number
                v-model="stopLossPct"
                :precision="2"
                :step="0.5"
                :min="0"
                :max="100"
                data-testid="exp-stop-loss"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="移动止损 %">
              <el-input-number
                v-model="trailingStopPct"
                :precision="2"
                :step="0.5"
                :min="0"
                :max="100"
                data-testid="exp-trailing-stop"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="最长持仓（分钟）">
              <el-input-number
                v-model="maxHoldingMinutes"
                :precision="0"
                :step="15"
                :min="0"
                :max="10080"
                data-testid="exp-max-holding"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="开盘预热（分钟）">
              <el-input-number
                v-model="openingWarmupMinutes"
                :precision="0"
                :step="5"
                :min="0"
                :max="390"
                data-testid="exp-opening-warmup"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="入场截止（分钟）">
              <el-input-number
                v-model="entryCutoffMinutes"
                :precision="0"
                :step="5"
                :min="0"
                :max="180"
                data-testid="exp-entry-cutoff"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="收盘平仓（分钟）">
              <el-input-number
                v-model="flattenMinutes"
                :precision="0"
                :step="5"
                :min="0"
                :max="180"
                data-testid="exp-flatten"
              />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="每日最多入场">
              <el-input-number
                v-model="maxEntriesPerSymbolPerDay"
                :precision="0"
                :step="1"
                :min="0"
                :max="1000"
                data-testid="exp-daily-entry-cap"
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="新鲜阈值穿越">
              <el-switch v-model="entryCrossingRequired" data-testid="exp-fresh-crossing" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-alert
          v-if="sessionWindowError"
          :title="sessionWindowError"
          type="warning"
          show-icon
          :closable="false"
          data-testid="exp-window-error"
        />
        <el-alert
          v-if="symbolMarketError"
          :title="symbolMarketError"
          type="warning"
          show-icon
          :closable="false"
          data-testid="exp-symbol-market-error"
        />

        <el-divider content-position="left">参数网格</el-divider>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="buy_low 候选值">
              <el-input v-model="buyLowGrid" placeholder="逗号分隔，如 178,180,182" data-testid="exp-grid-buy-low" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="sell_high 候选值">
              <el-input v-model="sellHighGrid" placeholder="逗号分隔，如 188,190,192" data-testid="exp-grid-sell-high" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="max_holding 候选">
          <div>
            <el-checkbox-group v-model="maxHoldingGrid" data-testid="exp-grid-max-holding">
              <el-checkbox-button
                v-for="minutes in MAX_HOLDING_GRID_OPTIONS"
                :key="minutes"
                :value="minutes"
              >
                {{ minutes }} 分钟
              </el-checkbox-button>
            </el-checkbox-group>
            <div class="field-help">可多选 15 / 30 / 45 / 60 分钟；未选时使用上方基准值。</div>
          </div>
        </el-form-item>

        <el-divider content-position="left">价格数据 (CSV)</el-divider>

        <el-form-item>
          <el-input
            v-model="csvText"
            type="textarea"
            :rows="6"
            placeholder="timestamp,open,high,low,close,volume&#10;2026-05-01T09:30:00Z,180,181,179,180.5,1000"
            data-testid="exp-csv"
          />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="running"
            :disabled="running || runBlockingError.length > 0"
            @click="handleRun"
            data-testid="exp-run-btn"
          >
            运行实验
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card
      v-if="currentExperimentId !== null"
      header="排行榜"
      style="margin-top: 16px"
      data-testid="leaderboard-card"
    >
      <div class="sort-controls" data-testid="sort-controls">
        <span style="margin-right: 8px">排序：</span>
        <el-select
          v-model="sortField"
          style="width: 200px"
          @change="reloadRuns"
          data-testid="sort-field-select"
        >
          <el-option label="总收益率" value="total_return_pct" />
          <el-option label="总PnL" value="total_pnl" />
          <el-option label="最大回撤" value="max_drawdown_pct" />
          <el-option label="胜率" value="win_rate" />
          <el-option label="Sharpe" value="sharpe_ratio" />
          <el-option label="Profit Factor" value="profit_factor" />
          <el-option label="盈亏比" value="profit_loss_ratio" />
          <el-option label="交易次数" value="trade_count" />
        </el-select>
        <el-select
          v-model="sortOrder"
          style="width: 100px; margin-left: 8px"
          @change="reloadRuns"
          data-testid="sort-order-select"
        >
          <el-option label="降序" value="desc" />
          <el-option label="升序" value="asc" />
        </el-select>
        <el-button-group style="margin-left: 16px">
          <el-button
            size="small"
            :type="statusFilter === 'all' ? 'primary' : ''"
            data-testid="exp-status-all"
            @click="statusFilter = 'all'"
          >全部 {{ runs.length }}</el-button>
          <el-button
            size="small"
            :type="statusFilter === 'COMPLETED' ? 'primary' : ''"
            data-testid="exp-status-done"
            @click="statusFilter = 'COMPLETED'"
          >完成 {{ completedCount }}</el-button>
          <el-button
            size="small"
            :type="statusFilter === 'FAILED' ? 'primary' : ''"
            data-testid="exp-status-failed"
            @click="statusFilter = 'FAILED'"
          >失败 {{ failedCount }}</el-button>
        </el-button-group>
      </div>

      <el-table
        :data="filteredRuns"
        v-loading="loadingRuns"
        style="margin-top: 12px"
        data-testid="leaderboard-table"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="run-detail" data-testid="run-detail">
              <div class="run-detail-section">
                <div class="run-detail-label">完整参数</div>
                <pre>{{ JSON.stringify(row.parameters, null, 2) }}</pre>
              </div>
              <div v-if="row.error" class="run-detail-section">
                <div class="run-detail-label">错误信息</div>
                <pre class="run-detail-error">{{ row.error }}</pre>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="参数" min-width="200">
          <template #default="{ row }">
            <span data-testid="run-params">{{ formatParams(row.parameters) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="总PnL" width="100">
          <template #default="{ row }">
            <span data-testid="run-pnl">{{ row.total_pnl.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="收益率" width="100">
          <template #default="{ row }">
            <span data-testid="run-return">{{ (row.total_return_pct * 100).toFixed(2) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="最大回撤" width="100">
          <template #default="{ row }">
            <span data-testid="run-drawdown">{{ (row.max_drawdown_pct * 100).toFixed(2) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="胜率" width="80">
          <template #default="{ row }">
            <span data-testid="run-win-rate">{{ (row.win_rate * 100).toFixed(1) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="Sharpe" width="80">
          <template #default="{ row }">
            <span data-testid="run-sharpe">{{ row.sharpe_ratio !== null ? row.sharpe_ratio.toFixed(2) : '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="PF" width="70">
          <template #default="{ row }">
            <span data-testid="run-profit-factor">{{ row.profit_factor !== null ? row.profit_factor.toFixed(2) : '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="盈亏比" width="80">
          <template #default="{ row }">
            <span data-testid="run-profit-loss-ratio">{{ row.profit_loss_ratio !== null ? row.profit_loss_ratio.toFixed(2) : '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="交易" width="60" prop="trade_count" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.status === RUNNER_STATUS.COMPLETED" type="success">完成</el-tag>
            <el-tag v-else-if="row.status === RUNNER_STATUS.FAILED" type="danger">失败</el-tag>
            <el-tag v-else>{{ row.status }}</el-tag>
            <span v-if="row.error" style="margin-left: 4px; color: #f56c6c; font-size: 12px" data-testid="run-error">
              {{ row.error }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button
              size="small"
              @click="onDraftToStrategy(row)"
              data-testid="run-draft-btn"
            >
              带回草稿
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-if="totalRuns > 20"
        style="margin-top: 12px; justify-content: flex-end"
        layout="prev, pager, next"
        :total="totalRuns"
        :page-size="20"
        :current-page="currentPage"
        @current-change="handlePageChange"
        data-testid="runs-pagination"
      />
    </el-card>

    <!-- LLM 评分卡片 -->
    <el-card
      v-if="currentExperimentId !== null"
      header="LLM 建议评分"
      style="margin-top: 16px"
      data-testid="llm-eval-card"
    >
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="evalSymbol" placeholder="AAPL.US" data-testid="eval-symbol" />
        </el-form-item>
        <el-form-item label="窗口(分)">
          <el-input-number v-model="evalHorizon" :min="5" :max="1440" data-testid="eval-horizon" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadEvaluations" data-testid="eval-load-btn">加载评分</el-button>
        </el-form-item>
      </el-form>
      <div v-if="evalResult">
        <p>样本数: {{ evalResult.sample_count }} | 命中率: {{ (evalResult.hit_rate * 100).toFixed(1) }}%</p>
        <el-table :data="evalResult.samples" size="small" style="margin-top: 8px">
          <el-table-column prop="created_at" label="时间" width="160" />
          <el-table-column prop="order_action" label="动作" width="100" />
          <el-table-column prop="tag" label="标签" width="100">
            <template #default="{ row }">
              <el-tag :type="tagType(row.tag)">{{ row.tag }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" />
        </el-table>
      </div>
    </el-card>

    <!-- 导出按钮 -->
    <div v-if="currentExperimentId !== null" style="margin-top: 16px">
      <el-button size="small" @click="onExport('json')" data-testid="exp-export-json">导出 JSON</el-button>
      <el-button size="small" @click="onExport('csv')" data-testid="exp-export-csv">导出 CSV</el-button>
      <el-button
        size="small"
        plain
        :disabled="runs.length === 0"
        data-testid="exp-export-page-csv"
        @click="onExportCurrentPage"
      >
        导出当前页 CSV
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { RUNNER_STATUS } from '../utils/constants'
import { downloadCsv } from '../utils/csv'
import {
  createStrategyExperiment,
  exportStrategyExperiment,
  getLLMEvaluations,
  runStrategyExperiment,
  listStrategyExperimentRuns,
} from '../api/strategy_experiments'
import type {
  BacktestParams,
  LLMEvaluationResponse,
  StrategyExperimentGrid,
  StrategyExperimentGridItem,
  StrategyExperimentRun,
} from '../types'

// ── Form state ──
const currentPage = ref(1)

const name = ref('')
const symbol = ref('')
const buyLow = ref(180)
const sellHigh = ref(190)
const buyLowGrid = ref('')
const sellHighGrid = ref('')
const quantity = ref(10)
const feeRate = ref(0.0005)
const slippagePct = ref(0)
const market = ref<'US' | 'HK'>('US')
const tradingSessionMode = ref<'ANY' | 'RTH_ONLY'>('ANY')
const stopLossPct = ref(0)
const trailingStopPct = ref(0)
const maxHoldingMinutes = ref(0)
const entryCutoffMinutes = ref(0)
const flattenMinutes = ref(0)
const openingWarmupMinutes = ref(0)
const entryCrossingRequired = ref(false)
const maxEntriesPerSymbolPerDay = ref(0)
const MAX_HOLDING_GRID_OPTIONS = [15, 30, 45, 60] as const
const maxHoldingGrid = ref<number[]>([])
const csvText = ref('')

const running = ref(false)

// ── Leaderboard state ──

const currentExperimentId = ref<number | null>(null)
const currentExperimentSymbol = ref('')
const runs = ref<StrategyExperimentRun[]>([])
const sortField = ref('total_return_pct')
const sortOrder = ref<'asc' | 'desc'>('desc')
const totalRuns = ref(0)
const loadingRuns = ref(false)
// Client-side status filter over the currently loaded page only. The leaderboard
// is server-paginated + server-sorted, so this is a within-page view toggle, not
// a full re-query.
const statusFilter = ref<'all' | 'COMPLETED' | 'FAILED'>('all')

const completedCount = computed(() => runs.value.filter((r) => r.status === RUNNER_STATUS.COMPLETED).length)
const failedCount = computed(() => runs.value.filter((r) => r.status === RUNNER_STATUS.FAILED).length)
const filteredRuns = computed(() => {
  if (statusFilter.value === 'all') return runs.value
  return runs.value.filter((r) => r.status === statusFilter.value)
})

const sessionWindowError = computed(() => {
  if (
    entryCutoffMinutes.value > 0
    && flattenMinutes.value > 0
    && flattenMinutes.value > entryCutoffMinutes.value
  ) {
    return '收盘前平仓窗口不能早于入场截止窗口'
  }
  return ''
})

const normalizedExperimentSymbol = computed(() => (
  symbol.value.trim().toUpperCase() || 'AAPL.US'
))

const symbolMarketError = computed(() => {
  const suffix = normalizedExperimentSymbol.value.endsWith('.HK')
    ? 'HK'
    : normalizedExperimentSymbol.value.endsWith('.US')
      ? 'US'
      : null
  if (suffix !== null && suffix !== market.value) {
    return `股票代码 ${normalizedExperimentSymbol.value} 与所选市场 ${market.value} 不一致`
  }
  return ''
})

const runBlockingError = computed(() => sessionWindowError.value || symbolMarketError.value)

// ── Helpers ──

function parseCsvValues(raw: string): number[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s))
    .filter((n) => !Number.isNaN(n))
}

function buildBaseParams(): BacktestParams {
  return {
    symbol: normalizedExperimentSymbol.value,
    market: market.value,
    trading_session_mode: tradingSessionMode.value,
    buy_low: buyLow.value,
    sell_high: sellHigh.value,
    short_selling: false,
    min_profit_amount: 5,
    max_daily_loss: 5000,
    max_consecutive_losses: 3,
    quantity: quantity.value,
    initial_cash: 100000,
    fee_rate: feeRate.value,
    fixed_fee: 0,
    slippage_pct: slippagePct.value,
    stop_loss_pct: stopLossPct.value,
    trailing_stop_pct: trailingStopPct.value,
    max_holding_minutes: maxHoldingMinutes.value,
    entry_cutoff_minutes_before_close: entryCutoffMinutes.value,
    flatten_minutes_before_close: flattenMinutes.value,
    opening_warmup_minutes: openingWarmupMinutes.value,
    entry_crossing_required: entryCrossingRequired.value,
    max_entries_per_symbol_per_day: maxEntriesPerSymbolPerDay.value,
  }
}

function buildParameterGrid(): StrategyExperimentGrid {
  const grid: StrategyExperimentGrid = {}

  const buyLows = parseCsvValues(buyLowGrid.value)
  if (buyLows.length > 0) {
    grid.buy_low = { values: buyLows } satisfies StrategyExperimentGridItem
  }

  const sellHighs = parseCsvValues(sellHighGrid.value)
  if (sellHighs.length > 0) {
    grid.sell_high = { values: sellHighs } satisfies StrategyExperimentGridItem
  }

  if (maxHoldingGrid.value.length > 0) {
    grid.max_holding_minutes = {
      values: [...maxHoldingGrid.value].sort((a, b) => a - b),
    } satisfies StrategyExperimentGridItem
  }

  grid.quantity = { value: quantity.value } satisfies StrategyExperimentGridItem
  grid.fee_rate = { value: feeRate.value } satisfies StrategyExperimentGridItem
  grid.slippage_pct = { value: slippagePct.value } satisfies StrategyExperimentGridItem

  return grid
}

function formatParams(params: Partial<BacktestParams>): string {
  const entries = Object.entries(params)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}=${v}`)
  return entries.join(', ')
}

function errorDetail(e: unknown): string {
  if (e && typeof e === 'object' && 'response' in e) {
    const resp = (e as Record<string, unknown>).response
    if (resp && typeof resp === 'object' && 'data' in resp) {
      const data = (resp as Record<string, unknown>).data
      if (data && typeof data === 'object' && 'detail' in data) {
        return String((data as Record<string, unknown>).detail ?? '')
      }
    }
  }
  return ''
}

// ── Actions ──

async function handleRun() {
  if (runBlockingError.value) {
    ElMessage.warning(runBlockingError.value)
    return
  }
  if (!csvText.value.trim()) {
    ElMessage.warning('请填入价格数据 CSV')
    return
  }

  running.value = true
  try {
    // 1) Create experiment
    const exp = await createStrategyExperiment({
      name: name.value.trim() || '未命名实验',
      symbol: symbol.value.trim().toUpperCase() || 'AAPL.US',
      base_params: buildBaseParams(),
      parameter_grid: buildParameterGrid(),
    })
    currentExperimentId.value = exp.id
    currentExperimentSymbol.value = exp.symbol
    ElMessage.success(`实验已创建，预计 ${exp.estimated_runs} 次回测`)

    // 2) Run experiment
    await runStrategyExperiment(exp.id, { csv_text: csvText.value })
    ElMessage.success('实验运行完成')

    // 3) Load runs
    await loadRuns()
  } catch (e: unknown) {
    ElMessage.error(errorDetail(e) || '实验执行失败')
  } finally {
    running.value = false
  }
}

let runsRequestSeq = 0

async function fetchRuns(page: number) {
  const expId = currentExperimentId.value
  if (expId === null) return

  currentPage.value = page

  const seq = ++runsRequestSeq
  loadingRuns.value = true
  try {
    const result = await listStrategyExperimentRuns(
      expId,
      { sort: sortField.value, order: sortOrder.value, page, page_size: 20 },
    )
    if (seq !== runsRequestSeq) return
    runs.value = result.items
    totalRuns.value = result.total
  } catch (e: unknown) {
    if (seq !== runsRequestSeq) return
    ElMessage.error(errorDetail(e) || '加载排行榜失败')
  } finally {
    if (seq === runsRequestSeq) {
      loadingRuns.value = false
    }
  }
}

async function loadRuns() {
  await fetchRuns(1)
}

async function reloadRuns() {
  await fetchRuns(1)
}

async function handlePageChange(page: number) {
  await fetchRuns(page)
}

// ── LLM Evaluation state ──
const evalSymbol = ref('')
const evalHorizon = ref(60)
const evalResult = ref<LLMEvaluationResponse | null>(null)
const router = useRouter()
function tagType(tag: string): string {
  switch (tag) {
    case 'EFFECTIVE':
      return 'success'
    case 'INEFFECTIVE':
      return 'info'
    case 'TOO_EARLY':
      return 'warning'
    case 'TOO_LATE':
      return 'warning'
    case 'RISKY':
      return 'danger'
    case 'INSUFFICIENT_DATA':
      return 'info'
    default:
      return 'info'
  }
}
async function loadEvaluations() {
  const sym = evalSymbol.value.trim().toUpperCase()
  if (!sym) {
    ElMessage.warning('请输入标的代码')
    return
  }
  try {
    const result = await getLLMEvaluations(sym, { horizon_minutes: evalHorizon.value })
    evalResult.value = result
  } catch (e: unknown) {
    ElMessage.error(errorDetail(e) || '加载评分失败')
  }
}
async function onExport(format: 'csv' | 'json') {
  const expId = currentExperimentId.value
  if (expId === null) return
  try {
    const data = await exportStrategyExperiment(expId, format)
    if (format === 'csv' && data instanceof Blob) {
      const url = window.URL.createObjectURL(data)
      const a = document.createElement('a')
      a.href = url
      a.download = `experiment-${expId}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => window.URL.revokeObjectURL(url), 1000)
    } else if (format === 'json') {
      let content: string
      if (data instanceof Blob) {
        content = await data.text()
      } else {
        content = JSON.stringify(data, null, 2)
      }
      const blob = new Blob([content], { type: 'application/json' })
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `experiment-${expId}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => window.URL.revokeObjectURL(url), 1000)
    }
  } catch (e: unknown) {
    ElMessage.error(errorDetail(e) || '导出失败')
  }
}
function onDraftToStrategy(row: StrategyExperimentRun) {
  const params = row.parameters
  const sym = currentExperimentSymbol.value
  const market = sym.toUpperCase().endsWith('.HK') ? 'HK' : 'US'
  router.push({
    path: '/strategy',
    query: {
      draftExperimentRunId: String(row.id),
      buy_low: String(params.buy_low ?? ''),
      sell_high: String(params.sell_high ?? ''),
      quantity: String(params.quantity ?? ''),
      fee_rate: String(params.fee_rate ?? ''),
      slippage_pct: String(params.slippage_pct ?? ''),
      market,
    },
  })
}

/** Client-side export of the currently loaded leaderboard page only.
 * Distinct from the server-side exportStrategyExperiment which dumps every
 * run in the experiment. Useful for quickly grabbing the visible ranking. */
function onExportCurrentPage() {
  if (runs.value.length === 0) return
  const rows = filteredRuns.value.map((r) => ({
    status: r.status,
    total_pnl: r.total_pnl?.toFixed(2) ?? '',
    total_return_pct: r.total_return_pct != null ? (r.total_return_pct * 100).toFixed(3) : '',
    max_drawdown_pct: r.max_drawdown_pct != null ? (r.max_drawdown_pct * 100).toFixed(3) : '',
    win_rate: r.win_rate != null ? (r.win_rate * 100).toFixed(1) : '',
    sharpe_ratio: r.sharpe_ratio?.toFixed(3) ?? '',
    profit_factor: r.profit_factor?.toFixed(3) ?? '',
    profit_loss_ratio: r.profit_loss_ratio?.toFixed(3) ?? '',
    trade_count: r.trade_count,
    buy_low: r.parameters?.buy_low ?? '',
    sell_high: r.parameters?.sell_high ?? '',
    quantity: r.parameters?.quantity ?? '',
    fee_rate: r.parameters?.fee_rate ?? '',
    slippage_pct: r.parameters?.slippage_pct ?? '',
    error: r.error ?? '',
  }))
  downloadCsv(`experiment_${currentExperimentId.value ?? 'x'}_page_${currentPage.value}.csv`, [
    { key: 'status', label: 'status' },
    { key: 'total_pnl', label: 'total_pnl' },
    { key: 'total_return_pct', label: 'total_return_pct' },
    { key: 'max_drawdown_pct', label: 'max_drawdown_pct' },
    { key: 'win_rate', label: 'win_rate' },
    { key: 'sharpe_ratio', label: 'sharpe_ratio' },
    { key: 'profit_factor', label: 'profit_factor' },
    { key: 'profit_loss_ratio', label: 'profit_loss_ratio' },
    { key: 'trade_count', label: 'trade_count' },
    { key: 'buy_low', label: 'buy_low' },
    { key: 'sell_high', label: 'sell_high' },
    { key: 'quantity', label: 'quantity' },
    { key: 'fee_rate', label: 'fee_rate' },
    { key: 'slippage_pct', label: 'slippage_pct' },
    { key: 'error', label: 'error' },
  ], rows)
  ElMessage.success(`已导出当前页 ${rows.length} 条`)
}
</script>

<style scoped>
.experiments-page h2 {
  margin-bottom: 16px;
}

.execution-fidelity-notice {
  margin-bottom: 18px;
}

.field-help {
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
  line-height: 1.4;
}

.sort-controls {
  display: flex;
  align-items: center;
}

.run-detail {
  padding: 8px 16px;
}

.run-detail-section {
  margin-bottom: 12px;
}

.run-detail-label {
  margin-bottom: 6px;
  color: #909399;
  font-size: 12px;
}

.run-detail pre {
  margin: 0;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.run-detail-error {
  color: #c43838;
}
</style>
