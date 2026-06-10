<template>
  <div class="orders-page">
    <div class="orders-header">
      <h3>{{ scope === 'today' ? '今日订单' : '历史订单' }}</h3>
      <el-radio-group v-model="scope" size="small" @change="handleScopeChange">
        <el-radio-button label="today">今日订单</el-radio-button>
        <el-radio-button label="history">历史订单</el-radio-button>
      </el-radio-group>
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { cancelOrder, getOrders } from '../api'
import type { OrderRecord } from '../types'
import { orderSideLabel, orderStatusLabel } from '../utils/labels'

const orders = ref<OrderRecord[]>([])
const loading = ref(false)
const cancellingOrderId = ref('')
const scope = ref<'today' | 'history'>('today')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

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

onMounted(() => loadOrders())

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
