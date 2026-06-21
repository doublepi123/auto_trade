<template>
  <div class="data-state" data-testid="data-state">
    <el-alert
      v-if="error"
      class="data-state-error"
      data-testid="data-state-error"
      type="error"
      :title="error"
      :closable="false"
      show-icon
    />
    <div v-else-if="loading" class="data-state-loading" data-testid="data-state-loading">
      <span class="data-state-spinner" aria-hidden="true" />
      <span>{{ loadingText }}</span>
    </div>
    <div v-else-if="empty" class="data-state-empty" data-testid="data-state-empty">
      <slot name="empty">
        <el-empty :description="emptyText" />
      </slot>
    </div>
    <slot v-else />
  </div>
</template>

<script setup lang="ts">
/**
 * Consistent loading / error / empty presentation for data panels.
 *
 * Render priority is error → loading → empty → default slot, so callers only
 * need to feed the three flags and provide their content as the default slot.
 * Any state can be customised via the matching named slot (#empty / #error /
 * #loading); the default slot renders the real content once everything is fine.
 */
withDefaults(
  defineProps<{
    loading?: boolean
    error?: string
    empty?: boolean
    emptyText?: string
    loadingText?: string
  }>(),
  {
    loading: false,
    error: '',
    empty: false,
    emptyText: '暂无数据',
    loadingText: '加载中…',
  },
)
</script>

<style scoped>
.data-state-error {
  margin-bottom: 12px;
}

.data-state-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px;
  color: #909399;
  font-size: 13px;
}

.data-state-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}

.data-state-spinner {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid #dcdfe6;
  border-top-color: #409eff;
  border-radius: 50%;
  animation: data-state-spin 0.8s linear infinite;
}

@keyframes data-state-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
