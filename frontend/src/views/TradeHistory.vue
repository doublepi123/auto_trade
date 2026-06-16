<template>
  <div class="orders-page">
    <div class="orders-header">
      <h3>{{ scope === 'today' ? '今日订单' : '历史订单' }}</h3>
      <el-radio-group v-model="scope" size="small" @change="handleScopeChange">
        <el-radio-button label="today">今日订单</el-radio-button>
        <el-radio-button label="history">历史订单</el-radio-button>
      </el-radio-group>
    </div>
    <div v-if="noteAnalytics && noteAnalytics.total > 0" class="analytics-card" data-testid="note-analytics">
      <div class="analytics-stat"><span>笔记</span><strong>{{ noteAnalytics.total }}</strong></div>
      <div class="analytics-stat"><span>已评分</span><strong>{{ noteAnalytics.rated_count }}</strong></div>
      <div class="analytics-stat">
        <span>平均评分</span>
        <el-rate :model-value="noteAnalytics.avg_rating ?? 0" disabled />
      </div>
      <div v-if="noteAnalytics.top_tags.length" class="analytics-stat analytics-tags">
        <span>热门标签</span>
        <div>
          <el-tag v-for="t in noteAnalytics.top_tags" :key="t.tag" size="small" effect="plain" style="margin: 2px">{{ t.tag }} · {{ t.count }}</el-tag>
        </div>
      </div>
    </div>
    <el-table :data="orders" stripe style="width: 100%" v-loading="loading">
      <el-table-column prop="broker_order_id" label="订单号" width="180" />
      <el-table-column prop="symbol" label="股票代码" width="120" />
      <el-table-column prop="source" label="来源" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.source === 'broker' ? 'primary' : 'info'">{{ row.source }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="side" label="方向" width="100">
        <template #default="{ row }">
          <el-tag :type="row.side === 'BUY' || row.side === 'BUY_TO_COVER' ? 'success' : 'danger'">{{ orderSideLabel(row.side) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="数量" width="120">
        <template #default="{ row }">
          <span>{{ row.quantity }}</span>
          <el-tag v-if="row.executed_quantity !== null && row.executed_quantity !== row.quantity" size="small" type="warning" style="margin-left: 4px">
            成交 {{ row.executed_quantity }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="price" label="价格" width="100">
        <template #default="{ row }">
          <span>${{ row.price }}</span>
          <span v-if="row.executed_price !== null && row.executed_price !== row.price" style="color: #e6a23c; font-size: 12px; margin-left: 4px">
            成交 ${{ row.executed_price }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ orderStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="200">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="笔记" width="120" align="center">
        <template #default="{ row }">
          <el-button
            v-if="row.id > 0"
            size="small"
            link
            :type="notesByOrder.has(row.id) ? 'primary' : ''"
            data-testid="trade-note-button"
            @click="openNoteDialog(row)"
          >
            {{ notesByOrder.has(row.id) ? '📝 查看' : '＋ 添加' }}
          </el-button>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button
            v-if="row.cancellable"
            type="danger"
            size="small"
            :loading="cancellingOrderId === row.broker_order_id"
            @click="handleCancel(row)"
          >
            撤单
          </el-button>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
    </el-table>
    <div class="orders-footer">
      <el-button @click="loadOrders(true)" :loading="loading">刷新</el-button>
      <el-pagination
        background
        layout="total, sizes, prev, pager, next"
        :total="total"
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="handlePageChange"
        @size-change="handlePageSizeChange"
      />
    </div>

    <el-dialog
      v-model="noteDialog.visible"
      :title="`交易笔记 · ${noteDialog.symbol} #${noteDialog.orderId}`"
      width="520px"
      data-testid="trade-note-dialog"
    >
      <el-form label-width="64px">
        <el-form-item label="笔记">
          <el-input
            v-model="noteDialog.note"
            type="textarea"
            :rows="5"
            maxlength="8000"
            show-word-limit
            data-testid="trade-note-input"
          />
        </el-form-item>
        <el-form-item label="标签">
          <el-select
            v-model="noteDialog.tags"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            placeholder="输入后回车添加"
            style="width: 100%"
            data-testid="trade-note-tags"
          >
            <el-option v-for="t in noteDialog.tags" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="评分">
          <el-rate v-model="noteDialog.rating" :max="5" data-testid="trade-note-rating" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button v-if="noteDialog.exists" type="danger" plain :loading="noteDialog.deleting" @click="handleDeleteNote">
          删除
        </el-button>
        <el-button @click="noteDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="noteDialog.saving" data-testid="trade-note-save" @click="handleSaveNote">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { cancelOrder, getOrders, getTradeNotes, getTradeNoteAnalytics, upsertTradeNote, deleteTradeNote } from '../api'
import type { OrderRecord, TradeNote, TradeNoteAnalytics } from '../types'
import { orderSideLabel, orderStatusLabel } from '../utils/labels'
import { resolveErrorMessage } from '../utils/error'

const orders = ref<OrderRecord[]>([])
const loading = ref(false)
const cancellingOrderId = ref('')
const scope = ref<'today' | 'history'>('today')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

// ---- Trade journal (notes/tags/rating per order) ----
const notesByOrder = ref<Map<number, TradeNote>>(new Map())
const noteAnalytics = ref<TradeNoteAnalytics | null>(null)
const noteDialog = reactive({
  visible: false,
  orderId: 0,
  symbol: '',
  note: '',
  tags: [] as string[],
  rating: 0,
  exists: false,
  saving: false,
  deleting: false,
})

async function loadOrders(refresh = false) {
  loading.value = true
  try {
    const data = await getOrders({
      scope: scope.value,
      page: page.value,
      page_size: pageSize.value,
      ...(scope.value === 'today' && refresh ? { refresh: true } : {}),
    })
    orders.value = data.items
    total.value = data.total
  } catch (e) {
    console.error('加载订单失败：', e)
    ElMessage.error('加载订单失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadOrders()
  loadNotes()
})

function handleScopeChange() {
  page.value = 1
  loadOrders()
}

function handlePageChange(nextPage: number) {
  page.value = nextPage
  loadOrders()
}

function handlePageSizeChange(nextPageSize: number) {
  pageSize.value = nextPageSize
  page.value = 1
  loadOrders()
}

async function handleCancel(row: OrderRecord) {
  cancellingOrderId.value = row.broker_order_id
  try {
    await cancelOrder(row.broker_order_id)
    ElMessage.success('撤单成功')
    await loadOrders()
  } catch (e) {
    console.error('撤单失败：', e)
    ElMessage.error('撤单失败')
  } finally {
    cancellingOrderId.value = ''
  }
}

async function loadNotes() {
  try {
    const page = await getTradeNotes({ page_size: 200 })
    const m = new Map<number, TradeNote>()
    for (const n of page.items) m.set(n.order_id, n)
    notesByOrder.value = m
  } catch {
    // Journal is supplementary; never block the page on it.
  }
  try {
    noteAnalytics.value = await getTradeNoteAnalytics()
  } catch {
    // analytics is supplementary
  }
}

function bumpNotes() {
  notesByOrder.value = new Map(notesByOrder.value)
}

async function openNoteDialog(row: OrderRecord) {
  if (row.id <= 0) return
  noteDialog.orderId = row.id
  noteDialog.symbol = row.symbol
  noteDialog.saving = false
  noteDialog.deleting = false
  const existing = notesByOrder.value.get(row.id)
  if (existing) {
    noteDialog.note = existing.note
    noteDialog.tags = [...existing.tags]
    noteDialog.rating = existing.rating ?? 0
    noteDialog.exists = true
  } else {
    noteDialog.note = ''
    noteDialog.tags = []
    noteDialog.rating = 0
    noteDialog.exists = false
  }
  noteDialog.visible = true
}

async function handleSaveNote() {
  noteDialog.saving = true
  try {
    const saved = await upsertTradeNote(noteDialog.orderId, {
      note: noteDialog.note,
      tags: noteDialog.tags,
      rating: noteDialog.rating === 0 ? null : noteDialog.rating,
    })
    notesByOrder.value.set(noteDialog.orderId, saved)
    bumpNotes()
    noteDialog.exists = true
    noteDialog.visible = false
    ElMessage.success('笔记已保存')
    loadNotes()
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '保存失败'))
  } finally {
    noteDialog.saving = false
  }
}

async function handleDeleteNote() {
  noteDialog.deleting = true
  try {
    await deleteTradeNote(noteDialog.orderId)
    notesByOrder.value.delete(noteDialog.orderId)
    bumpNotes()
    noteDialog.visible = false
    ElMessage.success('笔记已删除')
    loadNotes()
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '删除失败'))
  } finally {
    noteDialog.deleting = false
  }
}

function formatDateTime(value: string): string {
  if (!value) return '-'
  return new Date(value).toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function statusType(status: string): string {
  switch (status) {
    case 'FILLED': return 'success'
    case 'PARTIAL_FILLED': return 'warning'
    case 'SUBMITTED': return 'warning'
    case 'REJECTED': return 'danger'
    case 'CANCELLED': return 'info'
    default: return ''
  }
}
</script>

<style scoped>
.orders-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: calc(100vh - 120px);
  padding: 16px;
  background: #fff;
}

.orders-header,
.orders-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.orders-header h3 {
  margin: 0;
}

.muted {
  color: #9ca3af;
}

.analytics-card {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 24px;
  padding: 12px 14px;
  border: 1px solid #e1e7f0;
  border-radius: 8px;
  background: #f8fafc;
}

.analytics-stat span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.analytics-stat strong {
  font-size: 18px;
  color: #172033;
}

.analytics-tags div {
  margin-top: 2px;
}

@media (max-width: 720px) {
  .orders-header,
  .orders-footer {
    align-items: flex-start;
    flex-direction: column;
  }
}

@media (max-width: 520px) {
  .orders-page {
    padding: 8px;
    gap: 12px;
  }

  .orders-header h3 {
    font-size: 16px;
  }
}
</style>
