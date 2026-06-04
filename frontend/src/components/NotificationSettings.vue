<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useNotificationStream } from '../composables/useNotificationStream'

const { prefs, updatePreferences } = useNotificationStream()
const localSound = ref(prefs.value.soundEnabled)
const localMax = ref(prefs.value.criticalPersistMaxPerMinute)

function onSave() {
  updatePreferences({
    soundEnabled: localSound.value,
    criticalPersistMaxPerMinute: localMax.value,
  })
  ElMessage.success('通知偏好已保存')
}
</script>

<template>
  <el-card data-testid="notification-settings">
    <template #header>通知偏好</template>
    <el-form>
      <el-form-item label="声音提醒">
        <el-switch v-model="localSound" />
      </el-form-item>
      <el-form-item label="CRITICAL 持久化上限（条/分钟）">
        <el-input-number v-model="localMax" :min="1" :max="20" />
      </el-form-item>
      <el-button @click="onSave" data-testid="notification-save-btn">保存</el-button>
    </el-form>
  </el-card>
</template>
