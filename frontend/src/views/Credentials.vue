<template>
  <div>
    <h3>Credentials</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="220px" @submit.prevent="handleSave">
        <el-form-item label="Longbridge App Key">
          <el-input v-model="form.longbridge_app_key" placeholder="Leave blank to keep current app key" />
        </el-form-item>
        <el-form-item label="Longbridge App Secret">
          <el-input v-model="form.longbridge_app_secret" placeholder="Leave blank to keep current app secret" show-password />
        </el-form-item>
        <el-form-item label="Longbridge Access Token">
          <el-input v-model="form.longbridge_access_token" placeholder="Leave blank to keep current access token" show-password />
        </el-form-item>
        <el-form-item label="Server酱 SCT Key">
          <el-input v-model="form.sct_key" placeholder="Leave blank to keep current SCT key" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSave" :loading="saving" :disabled="loading">Save</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">Saved!</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getCredentials, updateCredentials } from '../api'

const form = ref({
  longbridge_app_key: '',
  longbridge_app_secret: '',
  longbridge_access_token: '',
  sct_key: '',
})

const saving = ref(false)
const loading = ref(true)
const saved = ref(false)

watch(form, () => {
  saved.value = false
}, { deep: true })

onMounted(async () => {
  try {
    const credentials = await getCredentials()
    form.value = {
      longbridge_app_key: credentials.longbridge_app_key,
      longbridge_app_secret: credentials.longbridge_app_secret,
      longbridge_access_token: credentials.longbridge_access_token,
      sct_key: credentials.sct_key,
    }
  } catch (e) {
    console.error('Failed to load credentials:', e)
    ElMessage.error('Failed to load credentials')
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  if (loading.value) return
  saving.value = true
  saved.value = false
  try {
    await updateCredentials(form.value)
    saved.value = true
  } catch (e) {
    console.error('Save failed:', e)
    ElMessage.error('Save failed')
  } finally {
    saving.value = false
  }
}
</script>
