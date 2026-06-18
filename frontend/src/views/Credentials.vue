<template>
  <div>
    <h3>凭证设置</h3>
    <el-card style="max-width: 600px" v-loading="loading">
      <el-alert
        type="info"
        title="已保存的凭证不会回显；留空字段会保留现有值，填写后保存将覆盖对应凭证。"
        show-icon
        :closable="false"
        style="margin-bottom: 16px"
      />
      <el-form :model="form" label-width="220px" @submit.prevent="handleSave" :disabled="loading || saving">
        <el-form-item label="长桥应用标识">
          <div class="credential-row">
            <el-input v-model="form.longbridge_app_key" placeholder="留空则保留当前应用标识" />
            <el-tag v-if="hasFlags.has_longbridge_app_key" class="credential-saved-tag" size="small" type="success">已保存</el-tag>
          </div>
        </el-form-item>
        <el-form-item label="长桥应用密钥">
          <div class="credential-row">
            <el-input v-model="form.longbridge_app_secret" placeholder="留空则保留当前应用密钥" show-password />
            <el-tag v-if="hasFlags.has_longbridge_app_secret" class="credential-saved-tag" size="small" type="success">已保存</el-tag>
          </div>
        </el-form-item>
        <el-form-item label="长桥访问令牌">
          <div class="credential-row">
            <el-input v-model="form.longbridge_access_token" placeholder="留空则保留当前访问令牌" show-password />
            <el-tag v-if="hasFlags.has_longbridge_access_token" class="credential-saved-tag" size="small" type="success">已保存</el-tag>
          </div>
        </el-form-item>
        <el-form-item label="Server酱推送密钥">
          <div class="credential-row">
            <el-input v-model="form.sct_key" placeholder="留空则保留当前推送密钥" show-password />
            <el-tag v-if="hasFlags.has_sct_key" class="credential-saved-tag" size="small" type="success">已保存</el-tag>
          </div>
        </el-form-item>

        <el-divider content-position="left">通知渠道</el-divider>
        <p style="margin: -6px 0 12px; color: #909399; font-size: 12px;">
          按严重度分级推送（Server酱 + Webhook）；至少保留一条以免风控告警丢失。
        </p>
        <div
          v-for="(channel, idx) in notificationChannels"
          :key="idx"
          class="channel-card"
          data-testid="notification-channel-row"
        >
          <el-form-item label="类型">
            <el-select v-model="channel.type" style="width: 160px" @change="onChannelTypeChange(channel)">
              <el-option label="Server酱" value="serverchan" />
              <el-option label="Webhook" value="webhook" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="channel.type === 'webhook'" label="URL">
            <el-input v-model="channel.url" placeholder="https://..." data-testid="webhook-url" />
          </el-form-item>
          <el-form-item v-if="channel.type === 'webhook'" label="Payload 模板">
            <el-input
              v-model="channel.template"
              type="textarea"
              :rows="3"
              placeholder='可选: 自定义 JSON 模板,支持 {title} {content} {severity} {timestamp} {source}'
              data-testid="webhook-template"
            />
            <div class="template-hint">
              可用变量：{title} {content} {severity} {timestamp} {source}
            </div>
            <div v-if="channel.template" class="template-preview" data-testid="webhook-template-preview">
              <div class="template-preview-title">预览</div>
              <pre>{{ previewTemplate(channel.template) }}</pre>
            </div>
          </el-form-item>
          <el-form-item label="级别下限">
            <el-select v-model="channel.severity_floor" style="width: 160px">
              <el-option label="INFO+" value="INFO" />
              <el-option label="WARNING+" value="WARNING" />
              <el-option label="仅 CRITICAL" value="CRITICAL" />
            </el-select>
          </el-form-item>
          <div class="channel-actions">
            <el-button plain size="small" :loading="channelTesting[idx]" data-testid="channel-test-btn" @click="testChannel(idx)">测试</el-button>
            <el-tag v-if="channelTestResults[idx]?.ok" type="success" size="small" data-testid="channel-test-success">通过</el-tag>
            <el-tag v-else-if="channelTestResults[idx]" type="danger" size="small" data-testid="channel-test-fail">{{ channelTestResults[idx]?.error }}</el-tag>
            <el-button type="danger" link native-type="button" @click="removeChannel(idx)" :disabled="notificationChannels.length <= 1">
              删除
            </el-button>
          </div>
        </div>
        <el-button plain type="primary" native-type="button" style="margin-bottom: 16px" data-testid="add-notification-channel" @click="addChannel">
          + 添加渠道
        </el-button>

        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
          <el-button
            plain
            type="info"
            native-type="button"
            :loading="testingConnection"
            :disabled="loading || saving"
            data-testid="test-credentials"
            @click="testConnection"
          >测试连接</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
          <el-tag v-if="error" type="danger" style="margin-left: 10px">{{ error }}</el-tag>
          <el-tag v-if="testResult?.ok" type="success" style="margin-left: 10px" data-testid="test-result-success">连接可用</el-tag>
          <el-tag v-else-if="testResult && !testResult.ok" type="danger" style="margin-left: 10px" data-testid="test-result-fail">{{ testResult.error }}</el-tag>
        </el-form-item>
      </el-form>
      <el-alert v-if="reloadWarning" type="warning" :title="reloadWarning" show-icon style="margin-top: 12px" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch, computed } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getCredentials, updateCredentials, testNotificationChannel } from '../api'
import type { NotificationChannel } from '../types'

const form = ref({
  longbridge_app_key: '',
  longbridge_app_secret: '',
  longbridge_access_token: '',
  sct_key: '',
})

const notificationChannels = ref<NotificationChannel[]>([{ type: 'serverchan', severity_floor: 'INFO' }])

const hasFlags = ref({
  has_longbridge_app_key: false,
  has_longbridge_app_secret: false,
  has_longbridge_access_token: false,
  has_sct_key: false,
})

const saving = ref(false)
const loading = ref(true)
const saved = ref(false)
const error = ref<string | null>(null)
const reloadWarning = ref<string | null>(null)
const savedSnapshot = ref(serializeSnapshot())
const testingConnection = ref(false)
const testResult = ref<{ ok: boolean; error?: string } | null>(null)
const channelTesting = ref<boolean[]>([])
const channelTestResults = ref<Array<{ ok: boolean; error?: string } | null>>([])

watch([form, notificationChannels], () => {
  if (isDirty()) {
    saved.value = false
    error.value = null
  }
}, { deep: true })

onMounted(async () => {
  try {
    const credentials = await getCredentials()
    hasFlags.value = {
      has_longbridge_app_key: credentials.has_longbridge_app_key,
      has_longbridge_app_secret: credentials.has_longbridge_app_secret,
      has_longbridge_access_token: credentials.has_longbridge_access_token,
      has_sct_key: credentials.has_sct_key,
    }
    const list = credentials.notification_channels ?? []
    notificationChannels.value = list.length
      ? JSON.parse(JSON.stringify(list)) as NotificationChannel[]
      : [{ type: 'serverchan', severity_floor: 'INFO' }]
    normalizeChannels(notificationChannels.value)
    channelTesting.value = notificationChannels.value.map(() => false)
    channelTestResults.value = notificationChannels.value.map(() => null)
    savedSnapshot.value = serializeSnapshot()
  } catch (e) {
    console.error('加载凭证失败：', e)
    error.value = '加载失败'
  } finally {
    loading.value = false
  }
})

onBeforeRouteLeave(() => {
  if (!isDirty()) return true
  return ElMessageBox.confirm('凭证表单中有未保存的输入，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})

function normalizeChannels(rows: NotificationChannel[]) {
  rows.forEach((ch) => {
    if (ch.type === 'serverchan') delete ch.url
    if (!ch.severity_floor) ch.severity_floor = 'INFO'
  })
}

function previewTemplate(template: string | undefined): string {
  if (!template) return ''
  const sample = {
    title: 'Auto Trade: 测试消息',
    content: '这是一条测试通知内容',
    severity: 'WARNING',
    timestamp: new Date().toISOString(),
    source: 'auto_trade',
  }
  return template
    .replace(/\{title\}/g, sample.title)
    .replace(/\{content\}/g, sample.content)
    .replace(/\{severity\}/g, sample.severity)
    .replace(/\{timestamp\}/g, sample.timestamp)
    .replace(/\{source\}/g, sample.source)
}

function onChannelTypeChange(ch: NotificationChannel) {
  if (ch.type === 'serverchan') delete ch.url
}

function addChannel() {
  notificationChannels.value.push({ type: 'serverchan', severity_floor: 'INFO' })
  channelTesting.value.push(false)
  channelTestResults.value.push(null)
}

function removeChannel(idx: number) {
  if (notificationChannels.value.length <= 1) return
  notificationChannels.value.splice(idx, 1)
  channelTesting.value.splice(idx, 1)
  channelTestResults.value.splice(idx, 1)
}

function serializeSnapshot(): string {
  return JSON.stringify({
    form: form.value,
    channels: notificationChannels.value,
  })
}

function isDirty(): boolean {
  return serializeSnapshot() !== savedSnapshot.value
}

function clearForm() {
  form.value = {
    longbridge_app_key: '',
    longbridge_app_secret: '',
    longbridge_access_token: '',
    sct_key: '',
  }
  savedSnapshot.value = serializeSnapshot()
}

/**
 * Send a smoke-test notification through the configured channels to verify
 * the saved credentials are still valid (longport token, webhook URLs, sct
 * key). This does NOT modify the saved state — it just exercises the
 * notifier pipeline.
 */
async function testConnection() {
  if (testingConnection.value) return
  testingConnection.value = true
  testResult.value = null
  try {
    const api = (await import('../api/client')).api
    const resp = await api.post('/api/credentials/test')
    testResult.value = resp.data?.ok
      ? { ok: true }
      : { ok: false, error: resp.data?.error ?? '测试失败' }
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : '测试连接失败'
    testResult.value = { ok: false, error: message }
  } finally {
    testingConnection.value = false
  }
}

async function testChannel(idx: number) {
  const channel = notificationChannels.value[idx]
  if (!channel || channelTesting.value[idx]) return
  channelTesting.value[idx] = true
  channelTestResults.value[idx] = null
  try {
    const result = await testNotificationChannel(channel)
    channelTestResults.value[idx] = result
    if (result.ok) {
      ElMessage.success('渠道测试通过')
    } else {
      ElMessage.error(result.error ?? '渠道测试失败')
    }
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : '渠道测试请求失败'
    channelTestResults.value[idx] = { ok: false, error: message }
    ElMessage.error(message)
  } finally {
    channelTesting.value[idx] = false
  }
}

async function handleSave() {
  if (loading.value) return
  if (notificationChannels.value.length < 1) {
    error.value = '至少保留一条通知渠道'
    return
  }
  for (const ch of notificationChannels.value) {
    if (ch.type === 'webhook') {
      const u = ch.url?.trim() ?? ''
      try {
        const parsed = new URL(u)
        if (!['http:', 'https:'].includes(parsed.protocol)) throw 0
      } catch {
        error.value = 'Webhook 必须填写有效的 http(s) URL'
        return
      }
    }
  }
  saving.value = true
  saved.value = false
  error.value = null
  try {
    const payload: Record<string, unknown> = Object.fromEntries(
      Object.entries(form.value).filter(([, value]) => value.trim() !== ''),
    )
    const channels = notificationChannels.value.map((c) => {
      if (c.type === 'serverchan') {
        return { type: 'serverchan' as const, severity_floor: c.severity_floor }
      }
      return { type: 'webhook' as const, severity_floor: c.severity_floor, url: c.url?.trim() ?? '', template: c.template?.trim() || undefined }
    })
    payload.notification_channels = channels
    const resp = await updateCredentials(payload)
    hasFlags.value = {
      has_longbridge_app_key: resp.has_longbridge_app_key,
      has_longbridge_app_secret: resp.has_longbridge_app_secret,
      has_longbridge_access_token: resp.has_longbridge_access_token,
      has_sct_key: resp.has_sct_key,
    }
    reloadWarning.value = resp.reload_warning ?? null
    const list = resp.notification_channels ?? []
    notificationChannels.value = list.length
      ? JSON.parse(JSON.stringify(list)) as NotificationChannel[]
      : [{ type: 'serverchan', severity_floor: 'INFO' }]
    normalizeChannels(notificationChannels.value)
    clearForm()
    saved.value = true
  } catch (e) {
    console.error('保存凭证失败：', e)
    error.value = '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.credential-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.credential-row :deep(.el-input) {
  flex: 1;
  min-width: 0;
}

.credential-saved-tag {
  flex: 0 0 auto;
}

.channel-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
  position: relative;
}

.channel-card :deep(.el-button.is-link) {
  position: absolute;
  top: 8px;
  right: 8px;
}

.channel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.template-hint {
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
}

.template-preview {
  margin-top: 8px;
  border-radius: 4px;
  background: #f5f7fa;
  padding: 10px;
}

.template-preview-title {
  margin-bottom: 6px;
  color: #6b7280;
  font-size: 12px;
}

.template-preview pre {
  margin: 0;
  color: #172033;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 520px) {
  :deep(.el-form-item__label) {
    float: none;
    display: block;
    text-align: left;
    padding: 0 0 4px;
    line-height: 1.4;
  }

  :deep(.el-form-item__content) {
    margin-left: 0 !important;
  }

  :deep(.el-form-item) {
    margin-bottom: 14px;
  }

  :deep(.el-input__wrapper),
  :deep(.el-input-number),
  :deep(.el-select) {
    width: 100% !important;
  }

  .credential-row {
    flex-wrap: wrap;
  }

  .credential-saved-tag {
    margin-top: 4px;
  }

  .channel-card {
    padding: 10px;
  }

  .channel-card :deep(.el-button.is-link) {
    position: static;
    float: right;
    margin-bottom: 8px;
  }
}
</style>
