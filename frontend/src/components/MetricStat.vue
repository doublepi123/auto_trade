<template>
  <div class="metric-stat" :class="{ 'metric-stat-spread': spread }" data-testid="metric-stat">
    <span class="metric-label">{{ label }}</span>
    <span class="metric-value-wrap">
      <strong
        class="metric-value"
        :class="[toneClass, valueClass]"
        :data-testid="valueTestId || undefined"
        >{{ value }}</strong
      >
      <small v-if="hint" class="metric-hint">{{ hint }}</small>
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

/**
 * Compact label / value / hint stat row.
 *
 * - `spread` lays the label out left and the value right (label : value),
 *   matching the common status-row pattern used in the health popover.
 * - `tone="sign"` colour-codes the value green/red from `toneValue`; pass
 *   `valueClass` for bespoke colouring (e.g. data-age freshness).
 */
const props = withDefaults(
  defineProps<{
    label: string
    value: string | number
    hint?: string
    spread?: boolean
    tone?: 'neutral' | 'sign'
    toneValue?: number | null
    valueClass?: string
    valueTestId?: string
  }>(),
  {
    hint: '',
    spread: false,
    tone: 'neutral',
    toneValue: null,
    valueClass: '',
    valueTestId: '',
  },
)

const toneClass = computed(() => {
  if (props.tone !== 'sign' || props.toneValue == null) return ''
  if (props.toneValue > 0) return 'metric-positive'
  if (props.toneValue < 0) return 'metric-negative'
  return ''
})
</script>

<style scoped>
.metric-stat {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 13px;
}
.metric-stat-spread {
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.metric-label {
  color: #6b7280;
}
.metric-value-wrap {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.metric-value {
  font-size: 14px;
}
.metric-hint {
  color: #909399;
  font-size: 12px;
}
.metric-positive {
  color: #67c23a;
}
.metric-negative {
  color: #f56c6c;
}
</style>
