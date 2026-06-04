<template>
  <div class="timeline-page">
    <div class="timeline-header">
      <div>
        <h3>决策时间线</h3>
        <p>行情、LLM、订单、风控与运维审计按时间倒序汇总</p>
      </div>
      <div class="timeline-actions">
        <el-radio-group v-model="sourceFilter" size="small" data-testid="timeline-source-filter" @change="onFilterChange">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="trade">交易</el-radio-button>
          <el-radio-button value="audit">审计</el-radio-button>
        </el-radio-group>
        <el-select
          v-model="selectedEventTypes"
          multiple
          clearable
          collapse-tags
          collapse-tags-tooltip
          placeholder="事件类型"
          data-testid="event-type-filter"
          style="width: 220px"
          @change="onFilterChange"
        >
          <el-option-group v-if="sourceFilter !== 'audit'" label="交易事件">
            <el-option label="LLM_ANALYSIS" value="LLM_ANALYSIS" />
            <el-option label="ORDER_FILLED" value="ORDER_FILLED" />
            <el-option label="ORDER_SKIPPED" value="ORDER_SKIPPED" />
            <el-option label="ORDER_REJECTED" value="ORDER_REJECTED" />
            <el-option label="ORDER_CANCELLED" value="ORDER_CANCELLED" />
            <el-option label="RISK_PAUSED" value="RISK_PAUSED" />
          </el-option-group>
          <el-option-group v-if="sourceFilter !== 'trade'" label="审计动作">
            <el-option label="START" value="START" />
            <el-option label="STOP" value="STOP" />
            <el-option label="PAUSE" value="PAUSE" />
            <el-option label="RESUME" value="RESUME" />
            <el-option label="KILL_SWITCH" value="KILL_SWITCH" />
            <el-option label="STRATEGY_UPDATE" value="STRATEGY_UPDATE" />
            <el-option label="CREDENTIALS_UPDATE" value="CREDENTIALS_UPDATE" />
            <el-option label="ORDER_CANCEL" value="ORDER_CANCEL" />
          </el-option-group>
        </el-select>
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
          <el-option label="非交易时段" value="SESSION" />
        </el-select>
        <el-button :loading="exporting === 'csv'" @click="handleExport('csv')">导出 CSV</el-button>
        <el-button :loading="exporting === 'json'" @click="handleExport('json')">导出 JSON</el-button>
        <el-button type="primary" :loading="loading" @click="loadEvents">刷新</el-button>
      </div>
    </div>

    <el-table
      :key="tableRefreshKey"
      :data="visibleEvents"
      stripe
      class="responsive-table"
      v-loading="loading"
      row-key="row_uid"
    >
      <el-table-column label="类型" width="72">
        <template #default="{ row }">
          <el-tag :type="row.source === 'audit' ? 'info' : 'primary'" effect="plain" size="small">
            {{ row.source === 'audit' ? '审计' : '交易' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="event_type" label="事件" min-width="120">
        <template #default="{ row }">
          <el-tag :type="eventType(row.event_type, row.status, row.source)" effect="plain">
            {{ timelineEventLabel(row) }}
          </el-tag>
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
          <el-tag v-if="row.source === 'trade' && row.status" :type="statusType(row.status)" effect="plain">{{ row.status }}</el-tag>
          <template v-else-if="row.source === 'audit'">
            <el-tag size="small" :type="auditSeverityTag(row.severity)">{{ row.severity || '-' }}</el-tag>
            <small v-if="row.result" style="margin-left: 6px; color:#909399">{{ row.result }}</small>
          </template>
          <span v-else>-</span>
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
      <el-table-column min-width="200" label="审计信息">
        <template #default="{ row }">
          <template v-if="row.source === 'audit'">
            <small>actor {{ (row.actor_hash || 'anon').slice(0, 8) }}</small><br >
            <small style="color:#909399">{{ row.source_ip || '-' }}</small>
          </template>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column prop="message" label="摘要" min-width="220" show-overflow-tooltip />
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
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { exportTradeEvents, getTradeEvents } from '../api'
import type { TimelineSource, TradeEventRecord } from '../types'
import { auditActionLabel, orderSideLabel, skipCategoryLabel, tradeEventTypeLabel } from '../utils/labels'
import { EVENT_TYPE } from '../utils/constants'

type Row = TradeEventRecord & { row_uid: string }

const events = ref<Row[]>([])
const loading = ref(false)
const exporting = ref<'csv' | 'json' | ''>('')
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selectedSkipCategory = ref('')
const sourceFilter = ref<TimelineSource>('all')
const selectedEventTypes = ref<string[]>([])
const tableRefreshKey = ref(0)

const visibleEvents = computed(() => {
  let rows = events.value
  if (selectedSkipCategory.value) {
    rows = rows.filter(
      (event) => event.payload?.skip_category === selectedSkipCategory.value,
    )
  }
  return rows
})

onMounted(loadEvents)

watch(sourceFilter, () => {
  selectedEventTypes.value = []
})

async function loadEvents() {
  loading.value = true
  try {
    const et = selectedEventTypes.value.length ? selectedEventTypes.value : undefined
    const data = await getTradeEvents({
      page: page.value,
      page_size: pageSize.value,
      source: sourceFilter.value,
      event_type: et,
    })
    events.value = data.items.map((item) => ({
      ...item,
      source: item.source ?? 'trade',
      row_uid: `${item.source ?? 'trade'}-${item.id}`,
    }))
    total.value = data.total
    tableRefreshKey.value++
  } catch (e) {
    console.error('加载决策时间线失败：', e)
    ElMessage.error('加载决策时间线失败')
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  page.value = 1
  loadEvents()
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
    ElMessage.success('导出已开始（仅包含交易事件）')
  } catch (e) {
    console.error('导出决策时间线失败：', e)
    ElMessage.error('导出失败')
  } finally {
    exporting.value = ''
  }
}

function timelineEventLabel(row: TradeEventRecord): string {
  return row.source === 'audit'
    ? auditActionLabel(row.event_type)
    : tradeEventTypeLabel(row.event_type)
}

function eventType(eventTypeValue: string, status: string, source: TradeEventRecord['source']): string {
  if (source === 'audit') {
    if (eventTypeValue === 'KILL_SWITCH') return 'danger'
    return 'info'
  }
  if (eventTypeValue === EVENT_TYPE.LLM_ANALYSIS) return status === 'FAILED' ? 'danger' : 'primary'
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

function auditSeverityTag(sev?: string | null): string {
  switch (sev) {
    case 'CRITICAL': return 'danger'
    case 'WARNING': return 'warning'
    default: return 'info'
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
  align-items: center;
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

@media (max-width: 520px) {
  .timeline-page {
    padding: 8px;
    gap: 12px;
  }

  .timeline-header h3 {
    font-size: 16px;
  }

  .timeline-header p {
    font-size: 12px;
  }

  .timeline-actions {
    gap: 6px;
  }

  .timeline-actions :deep(.el-select) {
    width: 140px !important;
  }
}
</style>
