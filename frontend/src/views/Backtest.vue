<template>
  <div class="backtest-page" data-testid="backtest-page">
    <div class="page-heading">
      <div>
        <h3>回测</h3>
        <p>{{ form.symbol || '未配置标的' }} · {{ form.short_selling ? '允许做空' : '仅做多' }}</p>
      </div>
      <div class="heading-tags">
        <el-tag effect="plain">{{ result ? `${result.metrics.trade_count} 笔信号` : '待运行' }}</el-tag>
        <el-tag :type="result ? metricTagType(result.metrics.total_pnl) : 'info'" effect="plain">
          {{ result ? signedCurrency(result.metrics.total_pnl) : '无结果' }}
        </el-tag>
      </div>
    </div>

    <section class="backtest-tool">
      <div class="tool-panel params-panel">
        <div class="panel-heading">
          <h4>参数</h4>
          <el-button size="small" plain @click="loadCurrentStrategy">同步当前策略</el-button>
        </div>
        <el-form label-width="132px" @submit.prevent="handleRun">
          <el-form-item label="股票代码">
            <el-input v-model="form.symbol" placeholder="AAPL.US" />
          </el-form-item>
          <div class="form-grid">
            <el-form-item label="买入价下限">
              <el-input-number v-model="form.buy_low" :precision="2" :step="0.01" :min="0.01" />
            </el-form-item>
            <el-form-item label="卖出价上限">
              <el-input-number v-model="form.sell_high" :precision="2" :step="0.01" :min="0.01" />
            </el-form-item>
            <el-form-item label="数量">
              <el-input-number v-model="form.quantity" :precision="0" :step="1" :min="1" />
            </el-form-item>
            <el-form-item label="初始资金">
              <el-input-number v-model="form.initial_cash" :precision="2" :step="1000" :min="1" />
            </el-form-item>
            <el-form-item label="最低盈利">
              <el-input-number v-model="form.min_profit_amount" :precision="2" :step="0.01" :min="0" />
            </el-form-item>
            <el-form-item label="止损百分比">
              <el-input-number v-model="form.stop_loss_pct" :precision="2" :step="0.5" :min="0" :max="100" />
            </el-form-item>
            <el-form-item label="单日最大亏损">
              <el-input-number v-model="form.max_daily_loss" :precision="2" :step="100" :min="1" />
            </el-form-item>
            <el-form-item label="连续亏损阈值">
              <el-input-number v-model="form.max_consecutive_losses" :precision="0" :step="1" :min="1" />
            </el-form-item>
            <el-form-item label="费率">
              <el-input-number v-model="form.fee_rate" :precision="5" :step="0.0005" :min="0" :max="0.1" />
            </el-form-item>
            <el-form-item label="固定费用">
              <el-input-number v-model="form.fixed_fee" :precision="2" :step="0.1" :min="0" />
            </el-form-item>
            <el-form-item label="滑点百分比">
              <el-input-number v-model="form.slippage_pct" :precision="3" :step="0.05" :min="0" :max="5" />
            </el-form-item>
            <el-form-item label="做空">
              <el-switch v-model="form.short_selling" />
            </el-form-item>
          </div>
        </el-form>
      </div>

      <div class="tool-panel data-panel">
        <div class="panel-heading">
          <h4>历史数据</h4>
          <div class="panel-actions">
            <input ref="fileInput" class="file-input" type="file" accept=".csv,text/csv" @change="handleFileUpload" />
            <el-button size="small" plain @click="fileInput?.click()">上传 CSV</el-button>
            <el-button size="small" plain @click="loadSampleCsv">载入示例</el-button>
          </div>
        </div>
        <el-input
          v-model="csvText"
          type="textarea"
          :rows="13"
          resize="vertical"
          data-testid="backtest-csv-input"
          placeholder="timestamp,open,high,low,close,volume"
        />
        <div class="run-row">
          <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
          <el-button
            type="primary"
            :loading="running"
            :disabled="!canRun"
            data-testid="run-backtest-button"
            @click="handleRun"
          >
            运行回测
          </el-button>
        </div>
      </div>
    </section>

    <section v-if="result" class="metrics-grid" data-testid="backtest-metrics">
      <div class="metric-item">
        <span>总收益</span>
        <strong :class="metricClass(result.metrics.total_pnl)">{{ signedCurrency(result.metrics.total_pnl) }}</strong>
        <small>{{ signedPercent(result.metrics.total_return_pct) }}</small>
      </div>
      <div class="metric-item">
        <span>胜率</span>
        <strong>{{ result.metrics.win_rate.toFixed(2) }}%</strong>
        <small>{{ result.metrics.winning_trades }} 胜 / {{ result.metrics.losing_trades }} 负</small>
      </div>
      <div class="metric-item">
        <span>最大回撤</span>
        <strong>{{ result.metrics.max_drawdown_pct.toFixed(2) }}%</strong>
        <small>权益低点压力</small>
      </div>
      <div class="metric-item">
        <span>平均持仓</span>
        <strong>{{ result.metrics.avg_holding_minutes.toFixed(1) }}m</strong>
        <small>{{ result.metrics.closed_trade_count }} 笔闭环</small>
      </div>
      <div class="metric-item">
        <span>费用</span>
        <strong>{{ formatCurrency(result.metrics.fees_paid, marketFromSymbol(form.symbol)) }}</strong>
        <small>{{ result.fee_sensitivity.length }} 档敏感性</small>
      </div>
      <div class="metric-item">
        <span>最终状态</span>
        <strong>{{ stateLabel(result.metrics.final_state) }}</strong>
        <small>{{ result.metrics.skipped_signals }} 个跳过信号</small>
      </div>
    </section>

    <BacktestChart v-if="result" :result="result" />

    <section v-if="result" class="result-grid">
      <div class="result-panel" data-testid="backtest-trades">
        <div class="section-title">
          <h4>交易明细</h4>
          <span>{{ result.trades.length }} 条</span>
        </div>
        <el-table :data="result.trades" size="small" class="responsive-table">
          <el-table-column prop="timestamp" label="时间" min-width="160">
            <template #default="{ row }">{{ formatDateTime(row.timestamp) }}</template>
          </el-table-column>
          <el-table-column prop="action" label="动作" min-width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="actionTagType(row.action)" effect="plain">{{ actionLabel(row.action) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="price" label="价格" min-width="100">
            <template #default="{ row }">{{ formatCurrency(row.price, marketFromSymbol(form.symbol)) }}</template>
          </el-table-column>
          <el-table-column prop="quantity" label="数量" min-width="80">
            <template #default="{ row }">{{ row.quantity.toFixed(0) }}</template>
          </el-table-column>
          <el-table-column prop="fee" label="费用" min-width="90">
            <template #default="{ row }">{{ formatCurrency(row.fee, marketFromSymbol(form.symbol)) }}</template>
          </el-table-column>
          <el-table-column prop="pnl" label="盈亏" min-width="100">
            <template #default="{ row }">
              <span :class="metricClass(row.pnl)">{{ signedCurrency(row.pnl) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="result-panel">
        <div class="section-title">
          <h4>跳过原因</h4>
          <span>{{ result.skipped_signals.length }} 条</span>
        </div>
        <div v-if="result.skipped_signals.length > 0" class="skip-list">
          <div v-for="signal in result.skipped_signals" :key="`${signal.timestamp}-${signal.action}`" class="skip-row">
            <strong>{{ actionLabel(signal.action) }} · {{ formatCurrency(signal.price, marketFromSymbol(form.symbol)) }}</strong>
            <span>{{ formatDateTime(signal.timestamp) }}</span>
            <el-tag v-if="signal.category" size="small" type="warning" effect="plain">
              {{ skipCategoryLabel(signal.category) }}
            </el-tag>
            <p>{{ signal.reason }}</p>
          </div>
        </div>
        <p v-else class="empty-note">暂无跳过信号</p>

        <h4 class="subsection-title">费用敏感性</h4>
        <el-table :data="result.fee_sensitivity" size="small" class="responsive-table">
          <el-table-column prop="fee_rate" label="费率" min-width="90">
            <template #default="{ row }">{{ (row.fee_rate * 100).toFixed(3) }}%</template>
          </el-table-column>
          <el-table-column prop="total_pnl" label="总收益" min-width="100">
            <template #default="{ row }">
              <span :class="metricClass(row.total_pnl)">{{ signedCurrency(row.total_pnl) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="max_drawdown_pct" label="回撤" min-width="80">
            <template #default="{ row }">{{ row.max_drawdown_pct.toFixed(2) }}%</template>
          </el-table-column>
        </el-table>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import BacktestChart from '../components/BacktestChart.vue'
import { getStrategy, runBacktest } from '../api'
import type { BacktestParams, BacktestResult } from '../types'
import { skipCategoryLabel } from '../utils/labels'
import { formatCurrency, marketFromSymbol } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'

const defaultParams: BacktestParams = {
  symbol: '',
  buy_low: 100,
  sell_high: 200,
  short_selling: false,
  min_profit_amount: 0,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
  quantity: 1,
  initial_cash: 100000,
  fee_rate: 0,
  fixed_fee: 0,
  slippage_pct: 0,
  stop_loss_pct: 0,
}

// Snapshot used by loadCurrentStrategy() to detect whether the user has
// modified any of the strategy-mirrored fields since the page was opened.
// Compared field-by-field so a re-sync of the same values is not flagged.
const initialForm: BacktestParams = { ...defaultParams }

const sampleCsv = `timestamp,open,high,low,close,volume
2026-05-22T10:00:00Z,150,160,99,105,1000
2026-05-22T10:01:00Z,120,140,110,130,1200
2026-05-22T10:02:00Z,150,201,145,200,1300
2026-05-22T10:03:00Z,180,190,120,130,900
2026-05-22T10:04:00Z,110,150,98,102,1100
2026-05-22T10:05:00Z,150,205,140,202,1250`

const form = ref<BacktestParams>({ ...defaultParams })
const csvText = ref(sampleCsv)
const result = ref<BacktestResult | null>(null)
const running = ref(false)
const error = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const canRun = computed(() => (
  csvText.value.trim().length > 0
  && form.value.buy_low > 0
  && form.value.sell_high > form.value.buy_low
  && form.value.quantity > 0
  && form.value.initial_cash > 0
))

async function loadCurrentStrategy() {
  // Detect unsaved edits to avoid silently clobbering the user's input.
  const baseline = initialForm
  const isDirty = (
    form.value.symbol !== baseline.symbol
    || form.value.buy_low !== baseline.buy_low
    || form.value.sell_high !== baseline.sell_high
    || form.value.short_selling !== baseline.short_selling
    || form.value.min_profit_amount !== baseline.min_profit_amount
    || form.value.max_daily_loss !== baseline.max_daily_loss
    || form.value.max_consecutive_losses !== baseline.max_consecutive_losses
  )
  if (isDirty) {
    try {
      await ElMessageBox.confirm(
        '当前表单有未保存的编辑，同步策略将覆盖这些修改。是否继续？',
        '同步当前策略',
        { confirmButtonText: '覆盖并同步', cancelButtonText: '取消', type: 'warning' }
      )
    } catch {
      return
    }
  }
  try {
    const strategy = await getStrategy()
    form.value = {
      ...form.value,
      symbol: strategy.symbol,
      buy_low: strategy.buy_low > 0 ? strategy.buy_low : form.value.buy_low,
      sell_high: strategy.sell_high > strategy.buy_low ? strategy.sell_high : form.value.sell_high,
      short_selling: strategy.short_selling,
      min_profit_amount: strategy.min_profit_amount,
      max_daily_loss: strategy.max_daily_loss,
      max_consecutive_losses: strategy.max_consecutive_losses,
    }
    // Refresh the baseline so the next "sync" doesn't re-flag the values
    // we just pulled from the live strategy. Without this, the second
    // click of the button always asks "覆盖并同步" even when nothing has
    // actually changed.
    initialForm.symbol = form.value.symbol
    initialForm.buy_low = form.value.buy_low
    initialForm.sell_high = form.value.sell_high
    initialForm.short_selling = form.value.short_selling
    initialForm.min_profit_amount = form.value.min_profit_amount
    initialForm.max_daily_loss = form.value.max_daily_loss
    initialForm.max_consecutive_losses = form.value.max_consecutive_losses
    ElMessage.success('已同步当前策略')
  } catch {
    ElMessage.error('同步失败')
  }
}

function loadSampleCsv() {
  csvText.value = sampleCsv
  error.value = ''
}

async function handleFileUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 10 * 1024 * 1024) {
    ElMessage.error('CSV 文件不能超过 10 MB')
    return
  }
  csvText.value = await file.text()
  input.value = ''
}

async function handleRun() {
  if (!canRun.value) return
  running.value = true
  error.value = ''
  try {
    result.value = await runBacktest({
      params: {
        ...form.value,
        symbol: form.value.symbol.trim().toUpperCase(),
      },
      csv_text: csvText.value,
    })
    ElMessage.success('回测完成')
  } catch (err) {
    error.value = resolveErrorMessage(err, '回测请求失败')
  } finally {
    running.value = false
  }
}

function formatNumber(value: number | null | undefined): string {
  return (value ?? 0).toFixed(2)
}

function signedCurrency(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function signedPercent(value: number | null | undefined): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+${amount}%`
  if (normalized < 0) return `-${amount}%`
  return `${amount}%`
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

function metricClass(value: number | null | undefined): string {
  const normalized = value ?? 0
  if (normalized > 0) return 'metric-positive'
  if (normalized < 0) return 'metric-negative'
  return ''
}

function metricTagType(value: number): string {
  if (value > 0) return 'success'
  if (value < 0) return 'danger'
  return 'info'
}

function stateLabel(value: string): string {
  if (value === 'long') return '持多'
  if (value === 'short') return '持空'
  return '空仓'
}

function actionLabel(action: string): string {
  const labels: Record<string, string> = {
    BUY: '买入',
    SELL: '卖出',
    SELL_SHORT: '开空',
    BUY_TO_COVER: '平空',
    STOP_LOSS_SELL: '止损卖出',
    STOP_LOSS_COVER: '止损平空',
  }
  return labels[action] ?? action
}

function actionTagType(action: string): string {
  if (action.startsWith('STOP_LOSS')) return 'warning'
  if (action === 'BUY' || action === 'BUY_TO_COVER') return 'success'
  if (action === 'SELL' || action === 'SELL_SHORT') return 'danger'
  return 'info'
}

onMounted(() => {
  loadCurrentStrategy().catch(() => void 0)
})
</script>

<style scoped>
.backtest-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-heading h3 {
  margin: 0;
}

.page-heading p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.heading-tags,
.panel-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.backtest-tool,
.result-grid {
  display: grid;
  grid-template-columns: minmax(360px, .95fr) minmax(420px, 1.15fr);
  gap: 12px;
}

.tool-panel,
.result-panel,
.metric-item {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #fff;
}

.tool-panel,
.result-panel {
  padding: 14px;
}

.panel-heading,
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-heading h4,
.section-title h4,
.subsection-title {
  margin: 0;
  color: #172033;
  font-size: 15px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 10px;
}

.params-panel :deep(.el-input-number) {
  width: 100%;
}

.file-input {
  display: none;
}

.run-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 12px;
}

.run-row :deep(.el-alert) {
  flex: 1 1 auto;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
}

.metric-item {
  min-height: 82px;
  padding: 10px 12px;
}

.metric-item span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.metric-item strong {
  display: block;
  margin-top: 5px;
  color: #172033;
  font-size: 20px;
  line-height: 1.1;
}

.metric-item small {
  display: block;
  margin-top: 5px;
  color: #7a8595;
  font-size: 12px;
}

.responsive-table {
  width: 100%;
}

.skip-list {
  display: grid;
  gap: 8px;
  margin-bottom: 16px;
}

.skip-row {
  border-radius: 6px;
  padding: 9px;
  background: #f8fafc;
}

.skip-row strong,
.skip-row span {
  display: block;
}

.skip-row strong {
  color: #172033;
  font-size: 13px;
}

.skip-row span {
  margin-top: 3px;
  color: #7a8595;
  font-size: 12px;
}

.skip-row p {
  margin: 6px 0 0;
  color: #4b5563;
  font-size: 12px;
  line-height: 1.45;
}

.subsection-title {
  margin: 16px 0 10px;
}

.empty-note {
  margin: 24px 0;
  color: #999;
  text-align: center;
}

.metric-positive {
  color: #14884f !important;
}

.metric-negative {
  color: #c43838 !important;
}

@media (max-width: 1280px) {
  .metrics-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 960px) {
  .backtest-tool,
  .result-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .page-heading,
  .panel-heading {
    flex-direction: column;
    align-items: flex-start;
  }

  .heading-tags,
  .panel-actions {
    justify-content: flex-start;
  }

  .form-grid,
  .metrics-grid {
    grid-template-columns: 1fr;
  }
}
</style>
