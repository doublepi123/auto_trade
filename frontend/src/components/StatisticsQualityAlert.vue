<template>
  <el-alert
    v-if="presentation"
    class="statistics-quality-alert"
    data-testid="statistics-quality-alert"
    :data-quality-status="presentation.status"
    :title="presentation.title"
    :description="presentation.description"
    :type="presentation.type"
    show-icon
    :closable="false"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StatisticsQuality } from '../types'

const props = defineProps<{
  quality?: StatisticsQuality | null
}>()

type AlertPresentation = {
  status: StatisticsQuality['status'] | 'UNKNOWN'
  title: string
  description: string
  type: 'warning' | 'error'
}

const presentation = computed<AlertPresentation | null>(() => {
  const quality = props.quality
  if (!quality) {
    return {
      status: 'UNKNOWN',
      title: '统计数据质量未知',
      description: '服务未返回质量状态，当前汇总可能不完整。',
      type: 'warning',
    }
  }

  if (quality.status === 'COMPLETE') return null

  const omittedDays = Math.max(0, quality.omitted_day_count)
  const unresolvedIssues = Math.max(0, quality.unresolved_issue_count)

  if (quality.status === 'KNOWN_EXCLUSIONS') {
    const exclusionCount = Math.max(0, quality.known_exclusion_count)
    return {
      status: quality.status,
      title: `统计已排除 ${exclusionCount} 笔已知历史数据`,
      description: '当前汇总已剔除对应成交，仅包含可验证的其余数据。',
      type: 'warning',
    }
  }

  if (quality.status === 'STALE_EXCLUSION') {
    return {
      status: quality.status,
      title: `历史排除记录已失效：已排除 ${omittedDays} 个交易日`,
      description: `发现 ${unresolvedIssues} 个待处理账本问题或失效排除，相关收益未计入汇总。`,
      type: 'error',
    }
  }

  return {
    status: quality.status,
    title: `统计数据不完整：已排除 ${omittedDays} 个交易日`,
    description: `发现 ${unresolvedIssues} 个待处理账本问题，相关收益未计入汇总。`,
    type: 'error',
  }
})
</script>

<style scoped>
.statistics-quality-alert {
  margin: 0;
}
</style>
