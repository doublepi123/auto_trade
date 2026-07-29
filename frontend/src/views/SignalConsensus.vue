<template>
  <div class="signal-consensus-page" data-testid="signal-consensus-page">
    <div class="page-header">
      <div>
        <h3>信号共识矩阵</h3>
        <p>横向比对范围引擎、Strategy v2、开盘动量、量化评分与 LLM 顾问的多源信号共识</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="symbolFilter"
          placeholder="标的过滤，逗号分隔"
          clearable
          data-testid="consensus-symbol-filter"
          style="width: 240px"
          @keyup.enter="reload"
          @clear="reload"
        />
        <el-button type="primary" :loading="loading" data-testid="consensus-refresh" @click="reload">刷新</el-button>
      </div>
    </div>

    <el-row :gutter="12" v-loading="loading">
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="一致看多" :value="summary?.agree_bullish ?? 0">
            <template #suffix>
              <span class="stat-suffix green">/ {{ summary?.total_symbols ?? 0 }}</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="一致看空" :value="summary?.agree_bearish ?? 0">
            <template #suffix>
              <span class="stat-suffix red">/ {{ summary?.total_symbols ?? 0 }}</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="分歧" :value="summary?.mixed ?? 0">
            <template #suffix>
              <span class="stat-suffix orange">/ {{ summary?.total_symbols ?? 0 }}</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <el-statistic title="数据不足" :value="summary?.insufficient ?? 0">
            <template #suffix>
              <span class="stat-suffix gray">/ {{ summary?.total_symbols ?? 0 }}</span>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
    </el-row>

    <el-table :data="rows" stripe v-loading="loading" class="responsive-table" data-testid="consensus-table">
      <el-table-column prop="symbol" label="标的" min-width="110" />
      <el-table-column label="范围引擎" min-width="130">
        <template #default="{ row }">
          <el-tag :type="signalTagType(row.range_engine.signal)" size="small" data-testid="consensus-vote">
            {{ signalLabel(row.range_engine.signal) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Strategy v2" min-width="130">
        <template #default="{ row }">
          <el-tag :type="signalTagType(row.strategy_v2.signal)" size="small">
            {{ signalLabel(row.strategy_v2.signal) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="开盘动量" min-width="130">
        <template #default="{ row }">
          <el-tag :type="signalTagType(row.opening_momentum.signal)" size="small">
            {{ signalLabel(row.opening_momentum.signal) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="量化评分" min-width="130">
        <template #default="{ row }">
          <el-tag :type="signalTagType(row.quant_score.signal)" size="small">
            {{ signalLabel(row.quant_score.signal) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="LLM顾问" min-width="130">
        <template #default="{ row }">
          <el-tag :type="signalTagType(row.llm_advisor.signal)" size="small">
            {{ signalLabel(row.llm_advisor.signal) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="共识" min-width="120">
        <template #default="{ row }">
          <el-tag :type="consensusTagType(row.consensus)" size="small" data-testid="consensus-tag">
            {{ consensusLabel(row.consensus) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="一致度" min-width="150">
        <template #default="{ row }">
          <el-progress
            :percentage="Math.round(row.agreement_score * 100)"
            :status="row.agreement_score >= 0.7 ? 'success' : undefined"
            data-testid="consensus-agreement"
          />
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-if="!loading && rows.length === 0" description="暂无共识数据" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getSignalMatrix, getSignalSummary } from '../api/signalConsensus'
import type { ConsensusRow, ConsensusSummary, SignalVote } from '../api/signalConsensus'
import { resolveErrorMessage } from '../utils/error'

type SignalKind = SignalVote['signal']

const rows = ref<ConsensusRow[]>([])
const summary = ref<ConsensusSummary | null>(null)
const loading = ref(false)
const symbolFilter = ref('')

function signalTagType(signal: SignalKind): string {
  switch (signal) {
    case 'BULLISH': return 'success'
    case 'BEARISH': return 'danger'
    case 'NEUTRAL': return 'info'
    default: return 'info'
  }
}

function signalLabel(signal: SignalKind): string {
  switch (signal) {
    case 'BULLISH': return '看多'
    case 'BEARISH': return '看空'
    case 'NEUTRAL': return '中性'
    default: return '无数据'
  }
}

function consensusTagType(consensus: ConsensusRow['consensus']): string {
  switch (consensus) {
    case 'AGREE_BULLISH': return 'success'
    case 'AGREE_BEARISH': return 'danger'
    case 'MIXED': return 'warning'
    default: return 'info'
  }
}

function consensusLabel(consensus: ConsensusRow['consensus']): string {
  switch (consensus) {
    case 'AGREE_BULLISH': return '一致看多'
    case 'AGREE_BEARISH': return '一致看空'
    case 'MIXED': return '分歧'
    default: return '数据不足'
  }
}

async function reload() {
  loading.value = true
  const symbols = symbolFilter.value.trim() || undefined
  try {
    const [matrix, sum] = await Promise.all([
      getSignalMatrix(symbols),
      getSignalSummary(),
    ])
    rows.value = matrix
    summary.value = sum
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载共识矩阵失败'))
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.signal-consensus-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.page-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.responsive-table {
  width: 100%;
}

.stat-suffix {
  font-size: 13px;
  color: #909399;
}

.stat-suffix.green { color: #67c23a; }
.stat-suffix.red { color: #f56c6c; }
.stat-suffix.orange { color: #e6a23c; }
.stat-suffix.gray { color: #909399; }

@media (max-width: 720px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
