<script setup lang="ts">
import { ref } from 'vue'
import { getProfitFactor, type ProfitFactorResult } from '../api/profitFactor'

const loading = ref(false)
const result = ref<ProfitFactorResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getProfitFactor({
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
</script>

<template>
  <div class="page-container">
    <h2>盈亏因子分解</h2>
    <p class="page-desc">按标的、交易规模拆解 Profit Factor，识别优势来源与集中度风险（灵感来自 QuantStats / Edgewonk）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分解</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Profit Factor" :value="result.overall.profit_factor ?? 0" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总利润" :value="result.overall.gross_profit" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总亏损" :value="result.overall.gross_loss" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="净 PnL" :value="result.overall.net_pnl" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>集中度</template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="Top3 盈利占比">{{ (result.concentration.top3_wins_share * 100).toFixed(1) }}%</el-descriptions-item>
            <el-descriptions-item label="Top3 亏损占比">{{ (result.concentration.top3_losses_share * 100).toFixed(1) }}%</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="14">
            <el-card>
              <template #header>按标的分解</template>
              <el-table :data="result.by_symbol" size="small" max-height="350">
                <el-table-column prop="segment" label="标的" width="120" />
                <el-table-column prop="trade_count" label="交易数" width="80" />
                <el-table-column prop="profit_factor" label="PF" width="80">
                  <template #default="{ row }">{{ row.profit_factor ?? '∞' }}</template>
                </el-table-column>
                <el-table-column prop="net_pnl" label="净 PnL" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: pnlColor(row.net_pnl) }">{{ row.net_pnl }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="win_rate" label="胜率" width="80">
                  <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="10">
            <el-card>
              <template #header>按规模分解</template>
              <el-table :data="result.by_size" size="small">
                <el-table-column prop="segment" label="规模" width="130" />
                <el-table-column prop="trade_count" label="交易数" width="80" />
                <el-table-column prop="profit_factor" label="PF" width="80">
                  <template #default="{ row }">{{ row.profit_factor ?? '∞' }}</template>
                </el-table-column>
                <el-table-column prop="net_pnl" label="净 PnL" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: pnlColor(row.net_pnl) }">{{ row.net_pnl }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
</style>
