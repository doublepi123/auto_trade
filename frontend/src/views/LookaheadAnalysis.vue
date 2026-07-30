<template>
  <div class="lookahead-analysis-page" data-testid="lookahead-analysis-page">
    <div class="page-header">
      <div>
        <h3>前瞻偏差分析</h3>
        <p>检测策略是否隐式依赖未来数据（灵感来自 Freqtrade lookahead-analysis）</p>
      </div>
      <div class="page-actions">
        <el-input v-model="symbol" placeholder="标的 (可选)" clearable style="width: 140px" size="small" />
        <el-input-number v-model="days" :min="1" :max="365" size="small" style="width: 100px" />
        <el-button type="primary" size="small" :loading="loading" @click="reload">分析</el-button>
      </div>
    </div>

    <div v-loading="loading">
      <template v-if="result">
        <el-row :gutter="12">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-tag :type="result.has_bias ? 'danger' : 'success'" size="large">
                {{ result.has_bias ? '存在偏差风险' : '无偏差证据' }}
              </el-tag>
              <el-statistic title="偏差评分" :value="result.bias_score" :precision="4" style="margin-top: 12px" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总平仓数" :value="result.total_exits" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="基线胜率" :value="result.baseline.win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="基线总盈亏" :value="result.baseline.total_pnl" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-card shadow="never" style="margin-top: 12px">
          <template #header>分片对比</template>
          <el-table :data="result.slices" size="small" max-height="320">
            <el-table-column prop="pct" label="数据比例 %" width="100" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column label="胜率" width="100">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column label="总盈亏" width="120">
              <template #default="{ row }">
                <span :class="row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'">{{ row.total_pnl.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="信号一致性" width="110">
              <template #default="{ row }">{{ (row.signal_consistency * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column label="胜率偏差" width="100">
              <template #default="{ row }">
                <span :class="row.win_rate_delta > 0.15 ? 'pnl-neg' : ''">{{ (row.win_rate_delta * 100).toFixed(1) }}%</span>
              </template>
            </el-table-column>
            <el-table-column label="盈亏偏差" width="100">
              <template #default="{ row }">{{ (row.pnl_delta * 100).toFixed(1) }}%</template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-alert
          :title="result.recommendation"
          :type="result.has_bias ? 'warning' : 'success'"
          show-icon
          :closable="false"
          style="margin-top: 12px"
        />
      </template>

      <el-empty v-else-if="!loading" description="点击「分析」开始检测" :image-size="60" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getLookaheadAnalysis } from '../api/lookaheadAnalysis'
import type { LookaheadResult } from '../api/lookaheadAnalysis'
import { resolveErrorMessage } from '../utils/error'

const result = ref<LookaheadResult | null>(null)
const loading = ref(false)
const symbol = ref('')
const days = ref(90)

async function reload() {
  loading.value = true
  try {
    result.value = await getLookaheadAnalysis({
      symbol: symbol.value || undefined,
      lookback_days: days.value,
    })
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '分析失败'))
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.lookahead-analysis-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}
.page-header h3 {
  margin: 0 0 4px;
}
.page-header p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}
.page-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.pnl-pos { color: #67c23a; }
.pnl-neg { color: #f56c6c; }
</style>
