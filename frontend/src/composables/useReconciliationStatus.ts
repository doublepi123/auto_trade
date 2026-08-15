import { ref, computed } from 'vue'
import { getReconciliationStatus, forceResumeReconciliation } from '../api/reconciliation'
import type { ReconciliationStatus } from '../types'

const defaultReconStatus: ReconciliationStatus = {
  reconciliation_gate: 'pending',
  last_evidence: null,
  recent_evidence: [],
  force_resume_available: false,
}

const reconciliationStatus = ref<ReconciliationStatus>({ ...defaultReconStatus })
const reconciliationLoading = ref(false)
let reconciliationPollInFlight = false

export function useReconciliationStatus() {
  async function fetchStatus() {
    if (reconciliationPollInFlight) return
    reconciliationPollInFlight = true
    reconciliationLoading.value = true
    try {
      reconciliationStatus.value = await getReconciliationStatus()
    } catch {
      // Keep previous values on error
    } finally {
      reconciliationPollInFlight = false
      reconciliationLoading.value = false
    }
  }

  async function forceResume(reason: string) {
    await forceResumeReconciliation(reason)
  }

  const gateLabel = computed(() => {
    switch (reconciliationStatus.value.reconciliation_gate) {
      case 'passed':
        return '已通过'
      case 'failed':
        return '未通过'
      case 'pending':
      default:
        return '等待中'
    }
  })

  const gateTagType = computed(() => {
    switch (reconciliationStatus.value.reconciliation_gate) {
      case 'passed':
        return 'success'
      case 'failed':
        return 'danger'
      case 'pending':
      default:
        return 'warning'
    }
  })

  return {
    reconciliationStatus,
    reconciliationLoading,
    fetchStatus,
    forceResume,
    gateLabel,
    gateTagType,
  }
}