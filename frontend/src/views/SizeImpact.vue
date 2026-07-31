<script setup lang="ts">
import { ref } from 'vue'
import { getSizeImpact, type SizeImpactResult } from '../api/sizeImpact'

const loading = ref(false)
const result = ref<SizeImpactResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getSizeImpact({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}

const trendLabel: Record<string, string> = {
  'increasing-returns': '规模递增收益',
  'diminishing-returns': '规模递减收益',
  stable: '稳定',
  insufficient: '样本不足',
}
</script>

<template>
  <div class="page-container">
    <h2>仓位规模影响</h2>
    <p class="page-desc">按仓位大小四分位分析单位收益效率，检测容量约束（灵感来自 Freqtrade / QuantConnect）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-alert :title="result.assessment" type="info" :closable="false" style="margin-top: 16px" />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">规模效率趋势</span>
                <span class="stat-value">{{ trendLabel[result.size_efficiency_trend] ?? result.size_efficiency_trend }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="四分位数" :value="result.quartiles.length" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>规模四分位绩效</template>
          <el-table :data="result.quartiles" size="small">
            <el-table-column prop="quartile" label="四分位" width="130" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="avg_quantity" label="平均数量" width="100" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="avg_pnl" label="平均 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.avg_pnl) }">{{ row.avg_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="win_rate" label="胜率" width="80">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column prop="pnl_per_unit" label="单位收益" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.pnl_per_unit) }">{{ row.pnl_per_unit }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
</style>
