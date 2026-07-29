<template>
  <div class="execution-quality-page" data-testid="execution-quality-page">
    <div class="page-header">
      <div>
        <h3>执行质量分析</h3>
        <p>查看订单成交率、拒绝原因与滑点表现</p>
      </div>
      <div class="header-actions">
        <el-select v-model="days" style="width: 120px" data-testid="execution-days-select" @change="loadAll">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 14 天" :value="14" />
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 60 天" :value="60" />
        </el-select>
        <el-button type="primary" :loading="loading" data-testid="execution-query-btn" @click="loadAll">刷新</el-button>
      </div>
    </div>

    <el-row v-loading="loading" :gutter="12" class="summary-row">
      <el-col :xs="12" :sm="6">
        <el-card class="summary-card">
          <div class="summary-value">{{ summary?.total_orders ?? 0 }}</div>
          <div class="summary-label">总订单</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card class="summary-card">
          <div class="summary-value positive">{{ (summary?.fill_rate_pct ?? 0).toFixed(1) }}%</div>
          <div class="summary-label">成交率</div>
          <div class="summary-sub">成交 {{ summary?.filled_orders ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card class="summary-card">
          <div class="summary-value">{{ (summary?.avg_fill_time_seconds ?? 0).toFixed(2) }}s</div>
          <div class="summary-label">平均成交时间</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card class="summary-card">
          <div class="summary-value" :class="{ negative: (summary?.rejection_rate_pct ?? 0) > 0 }">
            {{ (summary?.rejection_rate_pct ?? 0).toFixed(1) }}%
          </div>
          <div class="summary-label">拒绝率</div>
          <div class="summary-sub">拒绝 {{ summary?.rejected_orders ?? 0 }} · 撤单 {{ summary?.cancelled_orders ?? 0 }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="section-card">
      <template #header><span>拒绝原因</span></template>
      <div v-if="rejectionReasons.length" class="reason-tags">
        <el-tag
          v-for="r in rejectionReasons"
          :key="r.reason"
          type="danger"
          effect="plain"
          data-testid="execution-reason-tag"
        >
          {{ r.reason }} · {{ r.count }}
        </el-tag>
      </div>
      <el-empty v-else description="暂无拒绝记录" :image-size="60" />
    </el-card>

    <el-card class="section-card">
      <template #header><span>按标的汇总</span></template>
      <el-table :data="symbolRows" style="width: 100%" empty-text="暂无订单数据" data-testid="execution-symbol-table">
        <el-table-column prop="symbol" label="标的" min-width="140" sortable />
        <el-table-column prop="orders" label="订单" width="100" sortable />
        <el-table-column prop="fills" label="成交" width="100" sortable />
        <el-table-column prop="rejects" label="拒绝" width="100" sortable />
        <el-table-column label="成交率" min-width="120" sortable :sort-by="(row: SymbolRow) => row.fill_rate">
          <template #default="{ row }">
            <el-progress :percentage="Math.round(row.fill_rate * 100)" :stroke-width="12" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="section-card">
      <template #header><span>滑点分析</span></template>
      <el-table
        v-loading="slippageLoading"
        :data="slippage"
        style="width: 100%"
        empty-text="暂无滑点数据"
        data-testid="execution-slippage-table"
      >
        <el-table-column prop="symbol" label="标的" min-width="140" sortable />
        <el-table-column label="平均滑点" min-width="120" sortable :sort-by="(row: SlippageRow) => row.avg_slippage_pct">
          <template #default="{ row }">
            <span :class="slippageClass(row.avg_slippage_pct)">{{ row.avg_slippage_pct.toFixed(4) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="最大滑点" min-width="120" sortable :sort-by="(row: SlippageRow) => row.max_slippage_pct">
          <template #default="{ row }">
            <span :class="slippageClass(row.max_slippage_pct)">{{ row.max_slippage_pct.toFixed(4) }}%</span>
          </template>
        </el-table-column>
        <el-table-column prop="trade_count" label="交易笔数" width="120" sortable />
        <el-table-column label="方向偏差" min-width="120" sortable :sort-by="(row: SlippageRow) => row.direction_bias">
          <template #default="{ row }">
            <span :class="row.direction_bias >= 0 ? 'positive' : 'negative'">{{ row.direction_bias.toFixed(4) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getQualitySummary, getSlippageAnalysis } from '../api/executionQuality'
import type { QualitySummary, SlippageRow } from '../api/executionQuality'

const days = ref(30)
const loading = ref(false)
const slippageLoading = ref(false)
const summary = ref<QualitySummary | null>(null)
const slippage = ref<SlippageRow[]>([])

interface ReasonTag {
  reason: string
  count: number
}

const rejectionReasons = computed<ReasonTag[]>(() => {
  const reasons = summary.value?.rejection_reasons ?? {}
  return Object.entries(reasons)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count)
})

interface SymbolRow {
  symbol: string
  orders: number
  fills: number
  rejects: number
  fill_rate: number
}

const symbolRows = computed<SymbolRow[]>(() => {
  const bySymbol = summary.value?.by_symbol ?? {}
  return Object.entries(bySymbol).map(([symbol, bucket]) => ({
    symbol,
    orders: bucket.orders,
    fills: bucket.fills,
    rejects: bucket.rejects,
    fill_rate: bucket.orders > 0 ? bucket.fills / bucket.orders : 0,
  })).sort((a, b) => b.orders - a.orders)
})

function slippageClass(v: number): string {
  if (v > 0) return 'negative'
  if (v < 0) return 'positive'
  return ''
}

async function loadSummary() {
  loading.value = true
  try {
    summary.value = await getQualitySummary(days.value)
  } catch (e) {
    console.error('加载执行质量汇总失败：', e)
    ElMessage.error('加载执行质量汇总失败')
    summary.value = null
  } finally {
    loading.value = false
  }
}

async function loadSlippage() {
  slippageLoading.value = true
  try {
    slippage.value = await getSlippageAnalysis(days.value)
  } catch (e) {
    console.error('加载滑点分析失败：', e)
    ElMessage.error('加载滑点分析失败')
    slippage.value = []
  } finally {
    slippageLoading.value = false
  }
}

async function loadAll() {
  await Promise.all([loadSummary(), loadSlippage()])
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.execution-quality-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.summary-row {
  margin-bottom: 0;
}

.summary-card {
  text-align: center;
}

.summary-value {
  color: #172033;
  font-size: 24px;
  font-weight: 800;
  line-height: 1.2;
}

.summary-value.positive {
  color: #14884f;
}

.summary-value.negative {
  color: #c43838;
}

.summary-label {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.summary-sub {
  margin-top: 2px;
  color: #9ca3af;
  font-size: 11px;
}

.section-card {
  margin-bottom: 0;
}

.reason-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}
</style>
