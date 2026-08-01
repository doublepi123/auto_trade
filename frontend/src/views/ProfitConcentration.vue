<script setup lang="ts">
import { computed, ref } from 'vue'
import { getProfitConcentrationSummary, type ProfitConcentrationResult } from '../api/profitConcentration'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<ProfitConcentrationResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getProfitConcentrationSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const lorenzPath = computed(() => {
  if (!result.value?.pareto_curve?.length) return ''
  const pts = [{ x: 0, y: 0 }]
  for (const p of result.value.pareto_curve) {
    pts.push({ x: p.top_pct_trades, y: p.profit_share })
  }
  const w = 560
  const h = 200
  return pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${(p.x * w).toFixed(1)},${(h - p.y * h).toFixed(1)}`)
    .join(' ')
})

const diagPath = 'M0,200 L560,0'
</script>

<template>
  <div class="page-container">
    <h2>盈利集中度</h2>
    <p class="page-desc">利润在少数盈利交易中的集中程度：Pareto 份额、Lorenz 曲线与 Gini 系数（灵感来自 QuantStats）</p>

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
      <StatisticsQualityAlert :quality="result.statistics_quality" style="margin-top: 16px" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">Gini（盈利单）</span>
                <span class="stat-value">{{ result.gini_winners.toFixed(3) }}</span>
                <span class="stat-sub">越高越集中</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">单笔最大盈利占比</span>
                <span class="stat-value">{{ (result.top_trade_share * 100).toFixed(1) }}%</span>
                <span class="stat-sub">{{ result.top_trade_pnl.toFixed(2) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">Top 5 盈利单占比</span>
                <span class="stat-value">{{ (result.top5_share * 100).toFixed(1) }}%</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">毛利 / 毛损</span>
                <span class="stat-value text-green">{{ result.gross_profit.toFixed(0) }}</span>
                <span class="stat-sub text-red">{{ result.gross_loss.toFixed(0) }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>Lorenz 曲线（盈利单累计份额）</template>
          <svg viewBox="0 0 560 200" class="lorenz">
            <path :d="diagPath" stroke="#dcdfe6" stroke-dasharray="4 4" fill="none" />
            <path :d="lorenzPath" stroke="#409eff" stroke-width="2" fill="none" />
          </svg>
          <div class="axis-note">横轴：盈利单累计比例（Top N%）· 纵轴：利润累计份额</div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>Pareto 明细</template>
          <el-table :data="result.pareto_curve" size="small">
            <el-table-column label="Top 交易比例" width="140">
              <template #default="{ row }">{{ (row.top_pct_trades * 100).toFixed(0) }}%</template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易数" width="100" />
            <el-table-column label="利润份额">
              <template #default="{ row }">
                <div class="share-wrap">
                  <div class="share-bar" :style="{ width: (row.profit_share * 100).toFixed(1) + '%' }" />
                  <span>{{ (row.profit_share * 100).toFixed(1) }}%</span>
                </div>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: #606266; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
.lorenz { width: 100%; max-width: 560px; background: #fafafa; border: 1px solid #ebeef5; border-radius: 4px; }
.axis-note { font-size: 11px; color: #909399; margin-top: 6px; }
.share-wrap { display: flex; align-items: center; gap: 8px; }
.share-bar { height: 12px; background: #409eff; border-radius: 2px; min-width: 1px; }
</style>
