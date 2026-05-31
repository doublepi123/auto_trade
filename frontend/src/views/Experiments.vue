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
            :disabled="running"
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
      </div>

      <el-table
        :data="runs"
        v-loading="loadingRuns"
        style="margin-top: 12px"
        data-testid="leaderboard-table"
      >
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
        <el-table-column label="交易" width="60" prop="trade_count" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'COMPLETED'" type="success">完成</el-tag>
            <el-tag v-else-if="row.status === 'FAILED'" type="danger">失败</el-tag>
            <el-tag v-else>{{ row.status }}</el-tag>
            <span v-if="row.error" style="margin-left: 4px; color: #f56c6c; font-size: 12px" data-testid="run-error">
              {{ row.error }}
            </span>
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
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createStrategyExperiment,
  runStrategyExperiment,
  listStrategyExperimentRuns,
} from '../api/strategy_experiments'
import type {
  BacktestParams,
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
const csvText = ref('')

const running = ref(false)

// ── Leaderboard state ──

const currentExperimentId = ref<number | null>(null)
const runs = ref<StrategyExperimentRun[]>([])
const sortField = ref('total_return_pct')
const sortOrder = ref<'asc' | 'desc'>('desc')
const totalRuns = ref(0)
const loadingRuns = ref(false)

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
    symbol: symbol.value.trim().toUpperCase() || 'AAPL.US',
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
    stop_loss_pct: 0,
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
</script>

<style scoped>
.experiments-page h2 {
  margin-bottom: 16px;
}

.sort-controls {
  display: flex;
  align-items: center;
}
</style>
