<template>
  <div class="decision-replay-page" data-testid="decision-replay-page">
    <div class="page-header">
      <div>
        <h3>交易决策回放</h3>
        <p>查看单笔交易的完整决策链路（行情、LLM、风控、订单）</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="symbolFilter"
          placeholder="可选标的筛选"
          style="width: 180px"
          clearable
          data-testid="replay-symbol-input"
          @keyup.enter="loadTrades"
          @clear="loadTrades"
        />
        <el-button type="primary" :loading="listLoading" data-testid="replay-refresh-btn" @click="loadTrades">刷新列表</el-button>
      </div>
    </div>

    <el-row :gutter="12">
      <el-col :xs="24" :md="10">
        <el-card class="section-card">
          <template #header><span>可回放交易</span></template>
          <el-table
            v-loading="listLoading"
            :data="trades"
            style="width: 100%"
            empty-text="暂无可回放交易"
            highlight-current-row
            max-height="640"
            data-testid="replay-trade-list"
            @row-click="onTradeClick"
          >
            <el-table-column prop="trade_id" label="编号" width="80" sortable />
            <el-table-column prop="symbol" label="标的" min-width="110" sortable />
            <el-table-column label="方向" width="80">
              <template #default="{ row }">
                <el-tag :type="row.side === 'BUY' ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.side === 'BUY' ? '买入' : '卖出' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="盈亏" min-width="100" sortable :sort-by="(row: ReplayableTrade) => row.pnl">
              <template #default="{ row }">
                <span :class="row.pnl >= 0 ? 'positive' : 'negative'">{{ row.pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="event_count" label="事件数" width="90" sortable />
            <el-table-column label="进场时间" min-width="150">
              <template #default="{ row }">{{ formatDateTime(row.entry_time) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="14">
        <el-card class="section-card" v-loading="detailLoading">
          <template #header><span>决策链路</span></template>
          <el-empty v-if="!replay && !detailLoading" description="请在左侧选择一笔交易以查看决策链路" />
          <template v-else-if="replay">
            <el-descriptions :column="2" border class="replay-summary">
              <el-descriptions-item label="标的">{{ replay.symbol }}</el-descriptions-item>
              <el-descriptions-item label="市场">{{ replay.market }}</el-descriptions-item>
              <el-descriptions-item label="方向">
                <el-tag :type="replay.side === 'BUY' ? 'success' : 'danger'" size="small" effect="plain">
                  {{ replay.side === 'BUY' ? '买入' : '卖出' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="盈亏">
                <span :class="replay.pnl >= 0 ? 'positive' : 'negative'">{{ replay.pnl.toFixed(2) }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="进出场价格">
                {{ replay.entry_price.toFixed(4) }} → {{ replay.exit_price.toFixed(4) }}
              </el-descriptions-item>
              <el-descriptions-item label="持仓时间">
                {{ formatDateTime(replay.entry_time) }} → {{ formatDateTime(replay.exit_time) }}
              </el-descriptions-item>
            </el-descriptions>

            <el-timeline class="replay-timeline" data-testid="replay-timeline">
              <el-timeline-item
                v-for="(entry, idx) in replay.timeline"
                :key="idx"
                :timestamp="formatDateTime(entry.timestamp)"
                :color="eventColor(entry.event_type)"
                placement="top"
              >
                <div class="timeline-item-head">
                  <el-tag :color="eventTagColor(entry.event_type)" effect="dark" size="small">
                    {{ entry.event_type }}
                  </el-tag>
                  <el-tag v-if="entry.status" :type="statusTagType(entry.status)" effect="plain" size="small">
                    {{ entry.status }}
                  </el-tag>
                </div>
                <div class="timeline-message">{{ entry.message }}</div>
              </el-timeline-item>
            </el-timeline>
          </template>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { replayTrade, listReplayableTrades } from '../api/decisionReplay'
import type { TradeReplay, ReplayableTrade } from '../api/decisionReplay'

const symbolFilter = ref('')
const listLoading = ref(false)
const detailLoading = ref(false)
const trades = ref<ReplayableTrade[]>([])
const replay = ref<TradeReplay | null>(null)

function eventColor(eventType: string): string {
  switch (eventType) {
    case 'PRICE_UPDATE': return '#2f6fed'
    case 'LLM_ANALYSIS': return '#7c3aed'
    case 'RISK_CHECK': return '#e6a23c'
    case 'ORDER_SUBMIT':
    case 'ORDER_FILL': return '#14884f'
    case 'ENTRY_SKIP': return '#c43838'
    case 'TRADE_CLOSE': return '#909399'
    default: return '#909399'
  }
}

function eventTagColor(eventType: string): string {
  return eventColor(eventType)
}

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'SUCCESS':
    case 'FILLED':
    case 'PASSED':
      return 'success'
    case 'FAILED':
    case 'REJECTED':
    case 'SKIPPED':
      return 'danger'
    case 'PENDING':
      return 'warning'
    default: return 'info'
  }
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString()
}

async function loadTrades() {
  listLoading.value = true
  try {
    const trimmed = symbolFilter.value.trim()
    trades.value = await listReplayableTrades(trimmed ? { symbol: trimmed, limit: 200 } : { limit: 200 })
  } catch (e) {
    console.error('加载可回放交易失败：', e)
    ElMessage.error('加载可回放交易失败')
    trades.value = []
  } finally {
    listLoading.value = false
  }
}

async function onTradeClick(row: ReplayableTrade) {
  detailLoading.value = true
  try {
    replay.value = await replayTrade(row.trade_id)
  } catch (e) {
    console.error('加载交易回放失败：', e)
    ElMessage.error('加载交易回放失败')
    replay.value = null
  } finally {
    detailLoading.value = false
  }
}

onMounted(() => {
  loadTrades()
})
</script>

<style scoped>
.decision-replay-page {
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

.replay-summary {
  margin-bottom: 16px;
}

.replay-timeline {
  padding-left: 4px;
}

.timeline-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.timeline-message {
  color: #374151;
  font-size: 13px;
  line-height: 1.5;
}

.positive {
  color: #14884f;
}

.negative {
  color: #c43838;
}

@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
