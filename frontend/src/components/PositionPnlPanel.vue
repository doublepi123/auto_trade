<template>
  <section class="position-pnl-panel" data-testid="position-pnl-panel">
    <div class="panel-heading">
      <h4>持仓浮盈（未实现 P&amp;L）</h4>
      <el-button size="small" plain :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-alert
      v-else-if="result && !result.available"
      title="实时行情不可用，仅展示成本基础（浮盈按 0 计）"
      type="warning"
      show-icon
      :closable="false"
    />

    <div v-if="result && result.positions.length" class="pnl-summary">
      <div class="pnl-stat">
        <span>总浮盈</span>
        <strong :class="pnlClass(result.total_unrealized_pnl)">{{ signed(result.total_unrealized_pnl) }}</strong>
      </div>
      <div class="pnl-stat">
        <span>总回报</span>
        <strong :class="pnlClass(result.total_unrealized_pnl_pct ?? 0)">{{ pct(result.total_unrealized_pnl_pct) }}</strong>
      </div>
      <div class="pnl-stat">
        <span>成本基础</span>
        <strong>{{ money(result.total_cost_basis) }}</strong>
      </div>
    </div>

    <el-table
      v-if="result && result.positions.length"
      :data="result.positions"
      size="small"
      class="responsive-table"
      data-testid="position-pnl-table"
    >
      <el-table-column prop="symbol" label="标的" min-width="100" />
      <el-table-column label="数量" min-width="80">
        <template #default="{ row }">{{ row.quantity }}</template>
      </el-table-column>
      <el-table-column label="均价" min-width="90">
        <template #default="{ row }">{{ money(row.avg_entry_cost, row.symbol) }}</template>
      </el-table-column>
      <el-table-column label="现价" min-width="90">
        <template #default="{ row }">{{ row.last_price !== null ? money(row.last_price, row.symbol) : '—' }}</template>
      </el-table-column>
      <el-table-column label="浮盈" min-width="110">
        <template #default="{ row }">
          <span :class="pnlClass(row.unrealized_pnl)">{{ signed(row.unrealized_pnl) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="浮盈%" min-width="90">
        <template #default="{ row }">{{ pct(row.unrealized_pnl_pct) }}</template>
      </el-table-column>
    </el-table>
    <p v-else-if="result" class="empty-note">暂无持仓</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getPositionPnl } from '../api'
import type { PositionPnlResult } from '../types'
import { formatCurrency, marketFromSymbol } from '../utils/format'
import { resolveErrorMessage } from '../utils/error'

const result = ref<PositionPnlResult | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    result.value = await getPositionPnl()
  } catch (e) {
    error.value = resolveErrorMessage(e, '加载持仓浮盈失败')
  } finally {
    loading.value = false
  }
}

function money(v: number, symbol?: string): string {
  return formatCurrency(v, symbol ? marketFromSymbol(symbol) : 'US')
}

function signed(v: number): string {
  const a = Math.abs(v).toFixed(2)
  if (v > 0) return `+$${a}`
  if (v < 0) return `-$${a}`
  return `$${a}`
}

function pct(v: number | null): string {
  if (v === null) return '—'
  const a = Math.abs(v).toFixed(2)
  if (v > 0) return `+${a}%`
  if (v < 0) return `-${a}%`
  return `${a}%`
}

function pnlClass(v: number): string {
  if (v > 0) return 'pnl-positive'
  if (v < 0) return 'pnl-negative'
  return ''
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.position-pnl-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-heading h4 {
  margin: 0;
  font-size: 15px;
  color: #172033;
}

.pnl-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}

.pnl-stat span {
  display: block;
  color: #6b7280;
  font-size: 12px;
}

.pnl-stat strong {
  font-size: 18px;
  color: #172033;
}

.responsive-table {
  width: 100%;
}

.empty-note {
  margin: 16px 0;
  color: #999;
  text-align: center;
}

.pnl-positive {
  color: #14884f;
}

.pnl-negative {
  color: #c43838;
}
</style>
