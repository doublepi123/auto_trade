<template>
  <div>
    <h3>Trade History</h3>
    <el-table :data="orders" stripe style="width: 100%" v-loading="loading">
      <el-table-column prop="broker_order_id" label="Order ID" width="180" />
      <el-table-column prop="symbol" label="Symbol" width="120" />
      <el-table-column prop="side" label="Side" width="100">
        <template #default="{ row }">
          <el-tag :type="row.side === 'BUY' ? 'success' : 'danger'">{{ row.side }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="quantity" label="Quantity" width="120" />
      <el-table-column prop="price" label="Price" width="100" />
      <el-table-column prop="status" label="Status" width="120">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="Created At" width="200" />
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getOrders } from '../api'
import type { OrderRecord } from '../types'

const orders = ref<OrderRecord[]>([])
const loading = ref(false)

onMounted(async () => {
  loading.value = true
  try {
    orders.value = await getOrders(100)
  } catch (e) {
    console.error('Failed to load orders:', e)
    ElMessage.error('Failed to load orders')
  } finally {
    loading.value = false
  }
})

function statusType(status: string): string {
  switch (status) {
    case 'FILLED': return 'success'
    case 'SUBMITTED': return 'warning'
    case 'REJECTED': case 'CANCELLED': return 'info'
    default: return ''
  }
}
</script>
