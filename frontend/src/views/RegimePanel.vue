<template>
  <div class="regime-panel-page" data-testid="regime-panel-page">
    <div class="page-header">
      <div>
        <h3>市场状态面板</h3>
        <p>查看标的当前市场状态及历史状态变化</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="symbol"
          placeholder="例如 AAPL.US"
          style="width: 180px"
          clearable
          data-testid="regime-symbol-input"
          @keyup.enter="loadAll"
        />
        <el-button type="primary" :loading="loading" data-testid="regime-query-btn" @click="loadAll">查询</el-button>
      </div>
    </div>

    <el-card v-loading="loading" class="section-card">
      <template #header><span>当前状态</span></template>
      <el-empty v-if="!current && !loading" description="请输入标的代码后查询" />
      <template v-else-if="current">
        <div class="current-regime">
          <el-tag
            size="large"
            :type="regimeTagType(current.regime_label)"
            effect="dark"
            data-testid="regime-current-label"
          >{{ regimeLabelCn(current.regime_label) }}</el-tag>
          <div class="confidence-block">
            <div class="confidence-label">置信度</div>
            <el-progress
              :percentage="Math.round(current.confidence * 100)"
              :color="confidenceColor(current.confidence)"
              data-testid="regime-confidence"
            />
          </div>
        </div>
        <el-descriptions :column="2" border class="indicators-desc">
          <el-descriptions-item label="波动率水平">{{ current.indicators.volatility_level }}</el-descriptions-item>
          <el-descriptions-item label="趋势方向">{{ current.indicators.trend_direction }}</el-descriptions-item>
          <el-descriptions-item label="成交量状态">{{ current.indicators.volume_regime }}</el-descriptions-item>
          <el-descriptions-item label="价格偏离均值">
            {{ current.indicators.price_vs_mean_pct.toFixed(2) }}%
          </el-descriptions-item>
          <el-descriptions-item label="数据点数">{{ current.data_points }}</el-descriptions-item>
          <el-descriptions-item label="更新时间">{{ formatDateTime(current.as_of) }}</el-descriptions-item>
        </el-descriptions>
      </template>
    </el-card>

    <el-card class="section-card">
      <template #header>
        <div class="card-header-row">
          <span>历史状态</span>
          <el-select v-model="days" size="small" style="width: 120px" data-testid="regime-days-select" @change="loadHistory">
            <el-option label="近 7 天" :value="7" />
            <el-option label="近 14 天" :value="14" />
            <el-option label="近 30 天" :value="30" />
            <el-option label="近 60 天" :value="60" />
          </el-select>
        </div>
      </template>
      <el-table
        v-loading="historyLoading"
        :data="history"
        style="width: 100%"
        empty-text="暂无历史数据"
        data-testid="regime-history-table"
      >
        <el-table-column prop="date" label="日期" width="140" sortable />
        <el-table-column label="状态" min-width="160">
          <template #default="{ row }">
            <el-tag :type="regimeTagType(row.regime_label)" effect="plain" size="small">
              {{ regimeLabelCn(row.regime_label) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="均价" min-width="120" sortable :sort-by="(row: RegimeHistoryPoint) => row.avg_price">
          <template #default="{ row }">{{ row.avg_price.toFixed(4) }}</template>
        </el-table-column>
        <el-table-column label="波动率代理" min-width="140" sortable :sort-by="(row: RegimeHistoryPoint) => row.volatility_proxy">
          <template #default="{ row }">{{ row.volatility_proxy.toFixed(4) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getCurrentRegime, getRegimeHistory } from '../api/regime'
import type { CurrentRegime, RegimeHistoryPoint } from '../api/regime'

const symbol = ref('AAPL.US')
const days = ref(30)
const loading = ref(false)
const historyLoading = ref(false)
const current = ref<CurrentRegime | null>(null)
const history = ref<RegimeHistoryPoint[]>([])

function regimeTagType(label: string): 'success' | 'danger' | 'info' | 'warning' {
  switch (label) {
    case 'TRENDING_UP': return 'success'
    case 'TRENDING_DOWN': return 'danger'
    case 'RANGE_BOUND': return 'info'
    case 'HIGH_VOLATILITY': return 'warning'
    case 'LOW_VOLATILITY': return 'info'
    default: return 'info'
  }
}

function regimeLabelCn(label: string): string {
  switch (label) {
    case 'TRENDING_UP': return '上升趋势'
    case 'TRENDING_DOWN': return '下降趋势'
    case 'RANGE_BOUND': return '区间震荡'
    case 'HIGH_VOLATILITY': return '高波动'
    case 'LOW_VOLATILITY': return '低波动'
    case 'UNKNOWN': return '未知'
    default: return label
  }
}

function confidenceColor(c: number): string {
  if (c >= 0.7) return '#14884f'
  if (c >= 0.4) return '#e6a23c'
  return '#c43838'
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString()
}

async function loadCurrent() {
  loading.value = true
  try {
    current.value = await getCurrentRegime(symbol.value.trim())
  } catch (e) {
    console.error('加载当前市场状态失败：', e)
    ElMessage.error('加载当前市场状态失败')
    current.value = null
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  if (!symbol.value.trim()) {
    history.value = []
    return
  }
  historyLoading.value = true
  try {
    history.value = await getRegimeHistory(symbol.value.trim(), days.value)
  } catch (e) {
    console.error('加载历史状态失败：', e)
    ElMessage.error('加载历史状态失败')
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

async function loadAll() {
  if (!symbol.value.trim()) {
    ElMessage.warning('请输入标的代码')
    return
  }
  await Promise.all([loadCurrent(), loadHistory()])
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.regime-panel-page {
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

.section-card {
  margin-bottom: 0;
}

.current-regime {
  display: flex;
  align-items: center;
  gap: 24px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.confidence-block {
  flex: 1;
  min-width: 220px;
}

.confidence-label {
  margin-bottom: 6px;
  color: #6b7280;
  font-size: 13px;
}

.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.indicators-desc {
  margin-top: 8px;
}
</style>
