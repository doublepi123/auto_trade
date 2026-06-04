<template>
  <div class="review-page">
    <div class="review-header">
      <div>
        <h3>复盘工作台</h3>
        <p>分析 LLM 建议与真实交易结果，优化策略决策</p>
      </div>
    </div>

    <!-- Filters -->
    <el-card class="filter-card">
      <el-form :inline="true" @submit.prevent="handleSearch">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" style="width: 160px" />
        </el-form-item>
        <el-form-item label="开始日期">
          <el-date-picker v-model="form.from_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item label="结束日期">
          <el-date-picker v-model="form.to_date" type="date" value-format="YYYY-MM-DD" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="handleSearch">查询</el-button>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('json')">导出 JSON</el-button>
          <el-button plain :disabled="!reviewData || reviewData.days.length === 0" @click="handleExport('csv')">导出 CSV</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- Summary cards -->
    <template v-if="reviewData && reviewData.days.length > 0">
      <el-row :gutter="12" class="summary-row">
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value" :class="pnlClass">{{ signedCurrency(reviewData.total_pnl) }}</div>
            <div class="summary-label">总盈亏</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.total_trades }}</div>
            <div class="summary-label">总交易次数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value">{{ reviewData.days.length }}</div>
            <div class="summary-label">复盘天数</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="6">
          <el-card class="summary-card">
            <div class="summary-value error-tags">
              <el-tag v-for="tag in reviewData.all_error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
              <span v-if="reviewData.all_error_tags.length === 0" class="no-tag">无</span>
            </div>
            <div class="summary-label">错误标签</div>
          </el-card>
        </el-col>
      </el-row>

      <!-- Timeline -->
      <div class="timeline-section">
        <div v-for="day in reviewData.days" :key="day.date" class="day-card">
          <div class="day-header">
            <strong>{{ day.date }}</strong>
            <el-tag :type="day.daily_pnl >= 0 ? 'success' : 'danger'" effect="plain">{{ signedCurrency(day.daily_pnl) }}</el-tag>
            <el-tag v-if="day.trade_count > 0" type="info" effect="plain">{{ day.trade_count }} 笔交易</el-tag>
            <div class="day-tags">
              <el-tag v-for="tag in day.error_tags" :key="tag" type="warning" size="small">{{ tag }}</el-tag>
            </div>
          </div>

          <div class="day-body">
            <!-- LLM suggestions -->
            <div v-if="day.llm_interactions.length > 0" class="section-block">
              <div class="section-title">LLM 建议</div>
              <div v-for="llm in day.llm_interactions" :key="llm.id" class="item-row">
                <el-tag :type="llm.applied ? 'success' : 'info'" size="small">{{ llm.applied ? '已采纳' : '未采纳' }}</el-tag>
                <span>{{ llm.order_action }} {{ llm.symbol }}</span>
                <span class="muted">{{ formatTime(llm.created_at) }}</span>
              </div>
            </div>

            <!-- Orders -->
            <div v-if="day.orders.length > 0" class="section-block">
              <div class="section-title">订单执行</div>
              <div v-for="order in day.orders" :key="order.id" class="item-row">
                <el-tag :type="order.side === 'BUY' || order.side === 'BUY_TO_COVER' ? 'success' : 'danger'" size="small">{{ order.side }}</el-tag>
                <span>{{ order.quantity.toFixed(0) }} 股 @ ${{ order.executed_price ?? order.price }}</span>
                <el-tag :type="order.status === ORDER_STATUS.FILLED ? 'success' : 'warning'" size="small">{{ order.status }}</el-tag>
                <span class="muted">{{ formatTime(order.created_at) }}</span>
              </div>
            </div>

            <!-- Events -->
            <div v-if="day.events.length > 0" class="section-block">
              <div class="section-title">交易事件</div>
              <div v-for="event in day.events" :key="event.id" class="item-row">
                <el-tag :type="eventType(event.event_type)" size="small">{{ event.event_type }}</el-tag>
                <span>{{ event.message || '-' }}</span>
                <span class="muted">{{ formatTime(event.created_at) }}</span>
              </div>
            </div>

            <!-- Snapshots -->
            <div v-if="day.snapshots.length > 0" class="section-block">
              <div class="section-title">行情快照</div>
              <div v-for="snap in day.snapshots" :key="snap.id" class="item-row">
                <span>价格 ${{ snap.last_price }}</span>
                <span v-if="snap.daily_pnl !== 0" :class="snap.daily_pnl >= 0 ? 'positive' : 'negative'">{{ signedCurrency(snap.daily_pnl) }}</span>
                <span class="muted">{{ formatTime(snap.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- Empty state -->
    <el-empty v-else-if="searched && !loading" description="No data for this period, please adjust filters" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { getReview, exportReview } from '../api/review'
import type { ReviewResponse } from '../types'
import { ORDER_STATUS } from '../utils/constants'

const form = ref({
  symbol: 'AAPL.US',
  from_date: '',
  to_date: '',
})

const loading = ref(false)
const searched = ref(false)
const reviewData = ref<ReviewResponse | null>(null)

const pnlClass = computed(() => {
  if (!reviewData.value) return ''
  return reviewData.value.total_pnl >= 0 ? 'positive' : 'negative'
})

function handleSearch() {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) {
    ElMessage.warning('请填写完整的查询条件')
    return
  }
  loading.value = true
  searched.value = true
  getReview({
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
  })
    .then((res) => {
      reviewData.value = res.data
    })
    .catch(() => {
      ElMessage.error('查询复盘数据失败')
      reviewData.value = null
    })
    .finally(() => {
      loading.value = false
    })
}

function handleExport(fmt: 'json' | 'csv') {
  if (!form.value.symbol || !form.value.from_date || !form.value.to_date) return
  exportReview({
    symbol: form.value.symbol,
    from_date: form.value.from_date,
    to_date: form.value.to_date,
    format: fmt,
  })
    .then((res) => {
      const blob = new Blob([res.data], { type: fmt === 'json' ? 'application/json' : 'text/csv' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `review_${form.value.symbol.replace('.', '_')}_${form.value.from_date}_${form.value.to_date}.${fmt}`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      ElMessage.success(`导出 ${fmt.toUpperCase()} 成功`)
    })
    .catch(() => {
      ElMessage.error('导出失败')
    })
}

function eventType(eventTypeValue: string): string {
  switch (eventTypeValue) {
    case 'ORDER_FILLED': return 'success'
    case 'ORDER_CANCELLED': return 'info'
    case 'ORDER_REJECTED': return 'danger'
    case 'ORDER_SKIPPED': return 'warning'
    case 'RISK_PAUSED': return 'danger'
    case 'LLM_ANALYSIS': return 'primary'
    default: return ''
  }
}

function signedCurrency(value: number): string {
  const normalized = value ?? 0
  const amount = Math.abs(normalized).toFixed(2)
  if (normalized > 0) return `+$${amount}`
  if (normalized < 0) return `-$${amount}`
  return `$${amount}`
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
</script>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.review-header h3 {
  margin: 0;
}

.review-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.filter-card {
  margin-bottom: 0;
}

.summary-row {
  margin-bottom: 8px;
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

.summary-value.error-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 400;
}

.no-tag {
  color: #909399;
}

.summary-label {
  margin-top: 4px;
  color: #6b7280;
  font-size: 12px;
}

.timeline-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.day-card {
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  padding: 14px;
  background: #f7f9fc;
}

.day-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e1e7f0;
}

.day-header strong {
  color: #172033;
  font-size: 15px;
}

.day-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-left: auto;
}

.day-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-block {
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}

.section-title {
  margin-bottom: 6px;
  color: #4b5563;
  font-size: 12px;
  font-weight: 700;
}

.item-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid #f1f5f9;
}

.item-row:last-child {
  border-bottom: none;
}

.muted {
  color: #909399;
  font-size: 12px;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 520px) {
  .review-page {
    padding: 8px;
    gap: 12px;
  }

  .day-header {
    flex-direction: column;
    align-items: flex-start;
  }

  .day-tags {
    margin-left: 0;
  }
}
</style>
