<template>
  <div class="drawdown-analysis-page" data-testid="drawdown-analysis-page">
    <div class="page-header">
      <div>
        <h3>回撤分析</h3>
        <p>查看累计盈亏回撤及恢复情况</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="symbol"
          placeholder="可选标的筛选（留空为全部）"
          style="width: 200px"
          clearable
          data-testid="drawdown-symbol-input"
        />
        <el-select v-model="days" style="width: 120px" data-testid="drawdown-days-select" @change="loadAll">
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 60 天" :value="60" />
          <el-option label="近 90 天" :value="90" />
          <el-option label="近 180 天" :value="180" />
        </el-select>
        <el-button type="primary" :loading="loading" data-testid="drawdown-query-btn" @click="loadAll">查询</el-button>
      </div>
    </div>

    <el-alert
      v-if="summary?.is_in_drawdown"
      title="当前处于回撤中"
      type="warning"
      :closable="false"
      show-icon
      data-testid="drawdown-alert"
    />

    <el-row v-loading="loading" :gutter="12" class="summary-row">
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="summary-card">
          <div class="summary-value" :class="{ negative: (summary?.current_drawdown ?? 0) > 0 }">
            {{ ((summary?.current_drawdown ?? 0)).toFixed(2) }}
          </div>
          <div class="summary-label">当前回撤</div>
          <div class="summary-sub">({{ ((summary?.current_drawdown_pct ?? 0) * 100).toFixed(2) }}%)</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="summary-card">
          <div class="summary-value negative">{{ (summary?.max_drawdown ?? 0).toFixed(2) }}</div>
          <div class="summary-label">最大回撤</div>
          <div class="summary-sub">({{ ((summary?.max_drawdown_pct ?? 0) * 100).toFixed(2) }}%)</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="summary-card">
          <div class="summary-value">{{ summary?.max_drawdown_duration_days ?? 0 }}</div>
          <div class="summary-label">最大回撤持续天数</div>
          <div class="summary-sub">{{ summary?.max_drawdown_date || '-' }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="summary-card">
          <div class="summary-value">{{ summary?.recovery_count ?? 0 }}</div>
          <div class="summary-label">恢复次数</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="summary-card">
          <div class="summary-value">{{ (summary?.avg_recovery_days ?? 0).toFixed(1) }}</div>
          <div class="summary-label">平均恢复天数</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="section-card">
      <template #header><span>回撤时间线</span></template>
      <el-table
        v-loading="timelineLoading"
        :data="timeline"
        style="width: 100%"
        empty-text="暂无回撤数据"
        data-testid="drawdown-timeline-table"
      >
        <el-table-column prop="date" label="日期" width="130" sortable />
        <el-table-column label="累计盈亏" min-width="120" sortable :sort-by="(row: DrawdownTimelinePoint) => row.cumulative_pnl">
          <template #default="{ row }">
            <span :class="row.cumulative_pnl >= 0 ? 'positive' : 'negative'">{{ row.cumulative_pnl.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="峰值" min-width="120" sortable :sort-by="(row: DrawdownTimelinePoint) => row.peak_pnl">
          <template #default="{ row }">{{ row.peak_pnl.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="回撤金额" min-width="120" sortable :sort-by="(row: DrawdownTimelinePoint) => row.drawdown">
          <template #default="{ row }">
            <span :class="{ negative: row.drawdown > 0 }">{{ row.drawdown.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="回撤比例" min-width="120" sortable :sort-by="(row: DrawdownTimelinePoint) => row.drawdown_pct">
          <template #default="{ row }">
            <span :class="{ negative: row.drawdown_pct > 0 }">{{ (row.drawdown_pct * 100).toFixed(2) }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="是否回撤中" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_in_drawdown" type="warning" size="small">回撤中</el-tag>
            <el-tag v-else type="success" size="small">正常</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getDrawdownSummary, getDrawdownTimeline } from '../api/drawdownAnalysis'
import type { DrawdownSummary, DrawdownTimelinePoint } from '../api/drawdownAnalysis'

const symbol = ref('')
const days = ref(60)
const loading = ref(false)
const timelineLoading = ref(false)
const summary = ref<DrawdownSummary | null>(null)
const timeline = ref<DrawdownTimelinePoint[]>([])

function buildParams(): { symbol?: string; days: number } {
  const trimmed = symbol.value.trim()
  return trimmed ? { symbol: trimmed, days: days.value } : { days: days.value }
}

async function loadSummary() {
  loading.value = true
  try {
    summary.value = await getDrawdownSummary(buildParams())
  } catch (e) {
    console.error('加载回撤汇总失败：', e)
    ElMessage.error('加载回撤汇总失败')
    summary.value = null
  } finally {
    loading.value = false
  }
}

async function loadTimeline() {
  timelineLoading.value = true
  try {
    timeline.value = await getDrawdownTimeline(buildParams())
  } catch (e) {
    console.error('加载回撤时间线失败：', e)
    ElMessage.error('加载回撤时间线失败')
    timeline.value = []
  } finally {
    timelineLoading.value = false
  }
}

async function loadAll() {
  await Promise.all([loadSummary(), loadTimeline()])
}

onMounted(() => {
  loadAll()
})
</script>

<style scoped>
.drawdown-analysis-page {
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
  flex-wrap: wrap;
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

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 520px) {
  .drawdown-analysis-page {
    padding: 8px;
    gap: 12px;
  }
}
</style>
