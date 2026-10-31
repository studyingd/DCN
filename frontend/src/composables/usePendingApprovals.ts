import { ref } from 'vue'
import { agentAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { usePolling } from '@/composables/usePolling'

/**
 * 待审批动作计数(全局单例):顶栏徽标、自动化概览卡、实时诊断页共用。
 * 30s 轮询,仅对持 automation:manage 的用户启动(其他人没有审批入口)。
 */
const count = ref(0)
let started = false
// 30s 轮询统一走 usePolling(2026-10-31 收编);全局单例不随组件卸载停止,
// 页面隐藏时暂停(铃铛无人看),恢复可见立即补一轮
const { start: startPolling } = usePolling(refresh, { intervalMs: 30_000 })

async function refresh(): Promise<void> {
  try {
    const res = await agentAPI.listApprovals()
    count.value = res.data.total
  } catch {
    /* 轮询失败静默,下一轮再试 */
  }
}

export function usePendingApprovals() {
  const authStore = useAuthStore()
  if (!started && authStore.hasPermission('automation:manage')) {
    started = true
    void refresh()
    startPolling()
  }
  return { count, refresh }
}
