<template>
  <div>
    <h3>交易历史</h3>
    <el-table :data="orders" stripe style="width: 100%" v-loading="loading">
      <el-table-column prop="broker_order_id" label="订单号" width="180" />
      <el-table-column prop="symbol" label="股票代码" width="120" />
      <el-table-column prop="side" label="方向" width="100">
        <template #default="{ row }">
          <el-tag :type="row.side === 'BUY' || row.side === 'BUY_TO_COVER' ? 'success' : 'danger'">{{ orderSideLabel(row.side) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="数量" width="120" />
      <el-table-column prop="price" label="价格" width="100" />
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ orderStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="200" />
    </el-table>
    <el-button style="margin-top: 12px" @click="loadOrders" :loading="loading">刷新</el-button>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getOrders } from '../api'
import type { OrderRecord } from '../types'
import { orderSideLabel, orderStatusLabel } from '../utils/labels'

const orders = ref<OrderRecord[]>([])
const loading = ref(false)

async function loadOrders() {
  loading.value = true
  try {
    orders.value = await getOrders(100)
  } catch (e) {
    console.error('加载订单失败：', e)
    ElMessage.error('加载订单失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadOrders)

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
