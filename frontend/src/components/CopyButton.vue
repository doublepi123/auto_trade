<template>
  <el-button
    class="copy-button"
    :size="size"
    :link="link"
    :title="title"
    :data-testid="testId"
    :aria-label="`复制 ${label || value}`"
    @click="handleClick"
  >
    <el-icon><CopyDocument /></el-icon>
    <span v-if="label" class="copy-button-label">{{ label }}</span>
    <span v-else-if="copied" class="copy-button-feedback">已复制</span>
  </el-button>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'
import { copyText } from '../utils/clipboard'

const props = withDefaults(
  defineProps<{
    value: string
    label?: string
    title?: string
    size?: 'small' | 'default' | 'large'
    link?: boolean
    testId?: string
    feedback?: boolean
  }>(),
  {
    label: '',
    title: '复制',
    size: 'small',
    link: true,
    testId: '',
    feedback: true,
  },
)

const copied = ref(false)

async function handleClick(): Promise<void> {
  const ok = await copyText(props.value)
  if (ok) {
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1500)
    if (props.feedback) {
      ElMessage.success({ message: '已复制', duration: 1200 })
    }
  } else if (props.feedback) {
    ElMessage.error('复制失败，请手动选择文本')
  }
}
</script>

<style scoped>
.copy-button {
  padding: 0 4px;
  vertical-align: middle;
}
.copy-button-feedback {
  color: #67c23a;
}
</style>
