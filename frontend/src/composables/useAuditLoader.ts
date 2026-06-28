import { reactive, ref, type Ref } from 'vue'

/**
 * 通用审计数据加载器 — 消除重复的 filter + load 模式
 * Returns a reactive object so refs auto-unwrap in templates.
 */
export function useAuditLoader<T>(fetchFn: (params: Record<string, string | number>) => Promise<{ data: T[] }>) {
  const items = ref<T[]>([]) as Ref<T[]>
  const loading = ref(false)
  const userFilter = ref('')
  const dateRange = ref<[string, string] | null>(null)
  // Monotonic generation guard: a newer load() supersedes older in-flight ones,
  // so a slow earlier response can't overwrite the latest result (stale write).
  let _gen = 0

  async function load(extraParams: Record<string, string | number> = {}) {
    loading.value = true
    const gen = ++_gen
    try {
      const params: Record<string, string | number> = { ...extraParams }
      if (userFilter.value) params.username = userFilter.value
      if (dateRange.value) {
        params.start_date = dateRange.value[0]
        params.end_date = dateRange.value[1]
      }
      const res = await fetchFn(params)
      if (gen === _gen) {
        items.value = res.data
      }
    } catch {
      if (gen === _gen) {
        items.value = []
      }
      /* user will see empty list */
    } finally {
      if (gen === _gen) {
        loading.value = false
      }
    }
  }

  // Wrap in reactive so refs auto-unwrap in template v-model bindings
  return reactive({ items, loading, userFilter, dateRange, load })
}
