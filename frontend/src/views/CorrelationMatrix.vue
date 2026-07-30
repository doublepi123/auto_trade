<script setup lang="ts">
import { ref } from 'vue'
import { getCorrelationMatrix, type CorrelationResult } from '../api/correlation'

const loading = ref(false)
const result = ref<CorrelationResult | null>(null)
const lookbackDays = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getCorrelationMatrix({ lookback_days: lookbackDays.value })
  } finally {
    loading.value = false
  }
}

function cellColor(v: number): string {
  if (v > 0.5) return '#f56c6c'
  if (v > 0.2) return '#e6a23c'
  if (v < -0.5) return '#67c23a'
  if (v < -0.2) return '#409eff'
  return '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>相关性矩阵</h2>
    <p class="page-desc">标的间日 PnL 相关性热力图，识别集中度风险与分散化质量（灵感来自 QuantStats / VectorBT）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">计算</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert
        v-if="result.note"
        :title="result.note"
        type="info"
        :closable="false"
        style="margin-top: 16px"
      />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="标的数" :value="result.symbols.length" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="平均绝对相关" :value="result.avg_abs_correlation" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="分散化得分" :value="result.diversification_score" :precision="4" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>相关性热力图</template>
          <div class="heatmap-wrapper">
            <table class="heatmap-table">
              <thead>
                <tr>
                  <th></th>
                  <th v-for="s in result.symbols" :key="s">{{ s }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in result.matrix" :key="i">
                  <td class="row-label">{{ result.symbols[i] }}</td>
                  <td
                    v-for="(v, j) in row"
                    :key="j"
                    class="cell"
                    :style="{ color: cellColor(v) }"
                  >
                    {{ v.toFixed(2) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>相关性排名（按绝对值）</template>
          <el-table :data="result.pairs" size="small" max-height="400">
            <el-table-column prop="symbol_a" label="标的 A" width="120" />
            <el-table-column prop="symbol_b" label="标的 B" width="120" />
            <el-table-column prop="correlation" label="相关系数" width="120">
              <template #default="{ row }">
                <span :style="{ color: cellColor(row.correlation) }">
                  {{ row.correlation.toFixed(4) }}
                </span>
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
.heatmap-wrapper { overflow-x: auto; }
.heatmap-table { border-collapse: collapse; font-size: 12px; }
.heatmap-table th, .heatmap-table td { padding: 6px 10px; text-align: center; border: 1px solid #ebeef5; }
.heatmap-table th { background: #f5f7fa; font-weight: 600; }
.row-label { font-weight: 600; background: #f5f7fa; text-align: left !important; }
.cell { font-variant-numeric: tabular-nums; }
</style>
