import { ref } from 'vue'
import { ElMessage } from 'element-plus'

export function useApi<T = unknown>() {
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function execute(apiCall: () => Promise<{ data: T }>): Promise<T | null> {
    loading.value = true
    error.value = null
    try {
      const response = await apiCall()
      return response.data
    } catch (err: unknown) {
      let message = '请求失败'
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } }
        if (axiosErr.response?.data?.detail) {
          message = axiosErr.response.data.detail
        }
      } else if (err instanceof Error) {
        message = err.message
      }
      error.value = message
      ElMessage.error(message)
      return null
    } finally {
      loading.value = false
    }
  }

  return {
    loading,
    error,
    execute,
  }
}
