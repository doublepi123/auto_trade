<template>
  <section class="attribution-panel" data-testid="symbol-attribution-panel">
    <div class="panel-heading">
      <h4>按标的归因（已实现盈亏）</h4>
      <div class="heading-controls">
        <el-select v-model="days" size="small" style="width: 110px" @change="load">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
        </el-select>
        <el-button size="small" plain :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <StatisticsQualityAlert v-if="result" :quality="result.statistics_quality" />

    <div v-if="result" class="attribution-total">
      <span>合计已实现（净）</span>
      <strong :class="pnlClass(result.total_realized_pnl)">{{ signed(result.total_realized_pnl) }}</strong>
    </div>

    <el-table v-if="result && result.rows.length" :data="result.rows" stripe size="small" data-testid="symbol-attribution-table">
      <el-table-column prop="symbol" label="标的" width="120" />
      <el-table-column label="已实现盈亏" width="120">
        <template #default="{ row }">
          <span :class="pnlClass(row.realized_pnl)">{{ signed(row.realized_pnl) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="贡献占比" width="100">
        <template #default="{ row }">{{ (row.contribution_share * 100).toFixed(1) }}%</template>
      </el-table-column>
      <el-table-column prop="trade_count" label="往返" width="70" />
      <el-table-column label="期望" width="100">
        <template #default="{ row }">
          <span :class="pnlClass(expectancy(row))" data-testid="attribution-expectancy">
            {{ signed(expectancy(row)) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="胜率" width="80">
        <template #default="{ row }">{{ row.win_rate.toFixed(0) }}%</template>
      </el-table-column>
      <el-table-column label="表现" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.realized_pnl > 0" type="success" size="small">盈利</el-tag>
          <el-tag v-else-if="row.realized_pnl < 0" type="danger" size="small">亏损</el-tag>
          <el-tag v-else type="info" size="small">打平</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最佳/最差" width="160">
        <template #default="{ row }">
          <span class="pnl-positive">{{ signed(row.largest_win ?? 0) }}</span>
          <span class="muted"> / </span>
          <span class="pnl-negative">{{ signed(row.largest_loss ?? 0) }}</span>
        </template>
      </el-table-column>
    </el-table>
    <p v-else class="empty-note">暂无已实现成交。</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getPnlBySymbol } from '../api'
import type { SymbolAttributionResponse } from '../types'
import { resolveErrorMessage } from '../utils/error'
import StatisticsQualityAlert from './StatisticsQualityAlert.vue'

const result = ref<SymbolAttributionResponse | null>(null)
const loading = ref(false)
const error = ref('')
const days = ref(30)

async function load() {
  loading.value = true
  error.value = ''
  try {
    result.value = await getPnlBySymbol({ days: days.value })
  } catch (e) {
    error.value = resolveErrorMessage(e, '加载标的归因失败')
  } finally {
    loading.value = false
  }
}

function signed(v: number): string {
  const a = Math.abs(v).toFixed(2)
  return v > 0 ? `+$${a}` : v < 0 ? `-$${a}` : `$${a}`
}

/** Per-symbol expectancy = realized PnL per round trip. Reuses already-loaded
 * row fields only; returns 0 when there are no trades. */
function expectancy(row: { realized_pnl: number; trade_count: number }): number {
  if (!row.trade_count || row.trade_count <= 0) return 0
  return row.realized_pnl / row.trade_count
}

function pnlClass(v: number): string {
  return v > 0 ? 'pnl-positive' : v < 0 ? 'pnl-negative' : ''
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.attribution-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.panel-heading h4 {
  margin: 0;
  font-size: 15px;
  color: #172033;
}

.heading-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.attribution-total {
  display: flex;
  align-items: center;
  gap: 8px;
}

.attribution-total span {
  color: #6b7280;
  font-size: 12px;
}

.attribution-total strong {
  font-size: 16px;
}

.empty-note {
  color: #9ca3af;
  font-size: 13px;
  margin: 0;
}

.muted {
  color: #9ca3af;
}

.pnl-positive {
  color: #16a34a;
  font-weight: 600;
}

.pnl-negative {
  color: #dc2626;
  font-weight: 600;
}

@media (max-width: 520px) {
  .panel-heading {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
