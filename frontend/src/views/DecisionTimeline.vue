<template>
  <div class="timeline-page">
    <div class="timeline-header">
      <div>
        <h3>决策时间线</h3>
        <p>行情、LLM、订单、风控事件按时间倒序汇总</p>
      </div>
      <div class="timeline-actions">
        <el-select
          v-model="selectedSkipCategory"
          clearable
          placeholder="跳过原因"
          data-testid="skip-category-filter"
          style="width: 150px"
        >
          <el-option label="成本不足" value="FEE" />
          <el-option label="改价不显著" value="REPRICING" />
          <el-option label="LLM 冷却中" value="COOLDOWN" />
          <el-option label="风控阻断" value="RISK" />
          <el-option label="已有挂单" value="PENDING" />
          <el-option label="可用持仓不足" value="POSITION" />
        </el-select>
        <el-button :loading="exporting === 'csv'" @click="handleExport('csv')">导出 CSV</el-button>
        <el-button :loading="exporting === 'json'" @click="handleExport('json')">导出 JSON</el-button>
        <el-button type="primary" :loading="loading" @click="loadEvents">刷新</el-button>
      </div>
    </div>

    <el-table :data="visibleEvents" stripe class="responsive-table" v-loading="loading">
      <el-table-column prop="event_type" label="事件" min-width="120">
        <template #default="{ row }">
          <el-tag :type="eventType(row.event_type, row.status)" effect="plain">{{ tradeEventTypeLabel(row.event_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="symbol" label="标的" min-width="110">
        <template #default="{ row }">{{ row.symbol || '-' }}</template>
      </el-table-column>
      <el-table-column prop="broker_order_id" label="订单号" min-width="180">
        <template #default="{ row }">{{ row.broker_order_id || '-' }}</template>
      </el-table-column>
      <el-table-column prop="side" label="方向" min-width="100">
        <template #default="{ row }">{{ row.side ? orderSideLabel(row.side) : '-' }}</template>
      </el-table-column>
      <el-table-column prop="status" label="状态" min-width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" effect="plain">{{ row.status || '-' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="原因分类" min-width="120">
        <template #default="{ row }">
          <el-tag v-if="row.payload?.skip_category" type="warning" effect="plain">
            {{ skipCategoryLabel(String(row.payload.skip_category)) }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column prop="message" label="摘要" min-width="280" show-overflow-tooltip />
      <el-table-column prop="created_at" label="时间" min-width="190">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <div class="timeline-footer">
      <el-pagination
        background
        layout="total, sizes, prev, pager, next"
        :total="total"
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[20, 50, 100, 200]"
        @current-change="handlePageChange"
        @size-change="handlePageSizeChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { exportTradeEvents, getTradeEvents } from '../api'
import type { TradeEventRecord } from '../types'
import { orderSideLabel, skipCategoryLabel, tradeEventTypeLabel } from '../utils/labels'

const events = ref<TradeEventRecord[]>([])
const loading = ref(false)
const exporting = ref<'csv' | 'json' | ''>('')
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selectedSkipCategory = ref('')

const visibleEvents = computed(() => {
  if (!selectedSkipCategory.value) return events.value
  return events.value.filter((event) => event.payload?.skip_category === selectedSkipCategory.value)
})

onMounted(loadEvents)

async function loadEvents() {
  loading.value = true
  try {
    const data = await getTradeEvents({ page: page.value, page_size: pageSize.value })
    events.value = data.items
    total.value = data.total
  } catch (e) {
    console.error('加载决策时间线失败：', e)
    ElMessage.error('加载决策时间线失败')
  } finally {
    loading.value = false
  }
}

function handlePageChange(nextPage: number) {
  page.value = nextPage
  loadEvents()
}

function handlePageSizeChange(nextPageSize: number) {
  pageSize.value = nextPageSize
  page.value = 1
  loadEvents()
}

async function handleExport(format: 'csv' | 'json') {
  exporting.value = format
  try {
    const blob = await exportTradeEvents(format)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `trade-events.${format}`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    ElMessage.success('导出已开始')
  } catch (e) {
    console.error('导出决策时间线失败：', e)
    ElMessage.error('导出失败')
  } finally {
    exporting.value = ''
  }
}

function eventType(eventTypeValue: string, status: string): string {
  if (eventTypeValue === 'LLM_ANALYSIS') return status === 'FAILED' ? 'danger' : 'primary'
  if (eventTypeValue === 'RISK_PAUSED') return 'danger'
  if (eventTypeValue === 'RISK_AUTO_RESUMED') return 'success'
  if (eventTypeValue === 'ORDER_FILLED') return 'success'
  if (eventTypeValue === 'ORDER_CANCELLED') return 'info'
  if (eventTypeValue === 'ORDER_REJECTED') return 'danger'
  if (eventTypeValue === 'ORDER_SKIPPED') return 'warning'
  return 'warning'
}

function statusType(status: string): string {
  switch (status) {
    case 'SUCCESS':
    case 'FILLED':
    case 'RUNNING':
      return 'success'
    case 'FAILED':
    case 'REJECTED':
    case 'PAUSED':
      return 'danger'
    case 'CANCELLED':
      return 'info'
    default:
      return 'warning'
  }
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
</script>

<style scoped>
.timeline-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.timeline-header,
.timeline-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.timeline-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.timeline-actions :deep(.el-button) {
  margin-left: 0;
}

.timeline-header h3 {
  margin: 0;
}

.timeline-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.responsive-table {
  width: 100%;
}

@media (max-width: 720px) {
  .timeline-header,
  .timeline-footer {
    align-items: flex-start;
    flex-direction: column;
  }

  .timeline-actions {
    justify-content: flex-start;
  }
}
</style>
