import { ref, computed, watch, nextTick } from 'vue'

export interface FormStateOptions<T> {
  initial: T
  load: () => Promise<T>
  save: (data: T) => Promise<void>
}

export function useFormState<T extends Record<string, unknown>>(options: FormStateOptions<T>) {
  const form = ref<T>({ ...options.initial })
  const loading = ref(false)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref<string | null>(null)
  const savedSnapshot = ref('')

  const isDirty = computed(() => JSON.stringify(form.value) !== savedSnapshot.value)

  watch(form, () => {
    if (isDirty.value) {
      saved.value = false
    }
  }, { deep: true })

  async function load() {
    loading.value = true
    error.value = null
    try {
      const data = await options.load()
      form.value = { ...data }
      await nextTick()
      savedSnapshot.value = JSON.stringify(form.value)
      saved.value = false
    } catch (e) {
      error.value = '加载失败'
      console.error(e)
    } finally {
      loading.value = false
    }
  }

  async function save() {
    saving.value = true
    error.value = null
    try {
      await options.save(form.value)
      savedSnapshot.value = JSON.stringify(form.value)
      saved.value = true
    } catch (e) {
      error.value = '保存失败'
      console.error(e)
    } finally {
      saving.value = false
    }
  }

  return {
    form,
    loading,
    saving,
    saved,
    error,
    isDirty,
    load,
    save,
  }
}
