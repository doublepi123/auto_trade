import { ref } from 'vue'

export function useFormSaveState() {
  const loading = ref(true)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref<string | null>(null)
  function markDirty() { saved.value = false; error.value = null }
  function beginSave() { saving.value = true; saved.value = false; error.value = null }
  function saveSucceeded() { saving.value = false; saved.value = true }
  function saveFailed(message: string) { saving.value = false; saved.value = false; error.value = message }
  return { loading, saving, saved, error, markDirty, beginSave, saveSucceeded, saveFailed }
}
