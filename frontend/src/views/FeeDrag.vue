<script setup lang="ts">
import { computed, ref } from 'vue'
import { getFeeDragSummary, type FeeDragResult } from '../api/feeDrag'

const loading = ref(false)
const result = ref<FeeDragResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getFeeDragSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const maxDailyFee = computed(() => {
  if (!result.value?.daily_fees?.length) return 0
  return Math.max(...result.value.daily_fees.map((d) => d.fees))
})

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>费用拖累分析</h2>
    <p class="page-desc">手续费对毛利侵蚀程度：总费用、费用/毛利比、分标的与日趋势（灵感来自 Freqtrade / QuantStats）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="days" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总费用" :value="result.total_fees" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="单笔均费" :value="result.avg_fee_per_trade" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">费用/毛利比</span>
                <span class="stat-value text-red">
                  {{ result.fee_to_gross_win_ratio != null ? (result.fee_to_gross_win_ratio * 100).toFixed(1) + '%' : '—' }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">净 PnL</span>
                <span class="stat-value" :style="{ color: pnlColor(result.total_net_pnl) }">
                  {{ result.total_net_pnl.toFixed(2) }}
                </span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px" v-if="result.daily_fees.length">
          <template #header>每日费用</template>
          <div class="bar-chart">
            <div v-for="d in result.daily_fees" :key="d.date" class="bar-item" :title="`${d.date}: ${d.fees}`">
              <div class="bar" :style="{ height: maxDailyFee > 0 ? (d.fees / maxDailyFee) * 100 + '%' : '0%' }" />
              <span class="bar-label">{{ d.date.slice(5) }}</span>
            </div>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>分标的费用</template>
          <el-table :data="result.by_symbol" size="small" max-height="480">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="trades" label="成交数" width="80" />
            <el-table-column prop="fees" label="费用" width="100" />
            <el-table-column prop="gross_pnl" label="毛利" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.gross_pnl) }">{{ row.gross_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="net_pnl" label="净利" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.net_pnl) }">{{ row.net_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column label="费用/毛利" width="100">
              <template #default="{ row }">
                {{ row.fee_share_of_gross != null ? (row.fee_share_of_gross * 100).toFixed(1) + '%' : '—' }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card style="margin-top: 16px" v-if="Object.keys(result.fee_sources).length">
          <template #header>费用来源</template>
          <el-tag v-for="(count, src) in result.fee_sources" :key="src" style="margin-right: 8px">
            {{ src }}: {{ count }}
          </el-tag>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.text-red { color: #f56c6c; }
.bar-chart { display: flex; align-items: flex-end; gap: 2px; height: 140px; overflow-x: auto; }
.bar-item { display: flex; flex-direction: column; align-items: center; min-width: 18px; height: 100%; justify-content: flex-end; }
.bar { width: 12px; background: #e6a23c; border-radius: 2px 2px 0 0; }
.bar-label { font-size: 9px; color: #909399; transform: rotate(-45deg); white-space: nowrap; margin-top: 4px; }
</style>
