/**
 * PVE 异步任务(创建/克隆/销毁/快照)的进度跟踪。
 *
 * create/clone 等操作提交后 PVE 返回 UPID,任务在 PVE 侧异步执行——可能失败
 * (存储满/名称冲突/锁定),此前前端只显示「已提交」就再无下文。这个
 * composable 持有「PVE 任务」小弹窗的状态,2s 轮询 task-status 到终态:
 * 成功→触发 onSuccess(刷新列表)并自动关窗;失败→停在弹窗里展示 PVE 的
 * exitstatus 原文。网络抖动不中断轮询;10 分钟上限防呆;dispose 清定时器。
 */
import { ref } from 'vue'

import { pveAPI } from '@/api'

export interface PveTaskTrackOptions {
  connId: number
  node: string
  upid: string
  /** 弹窗标题,如「创建虚拟机 web-01 (VMID 105)」 */
  title: string
  /** 任务成功后的回调(通常刷新资源列表) */
  onSuccess?: () => void
}

/** 轮询间隔(ms)与跟踪上限(s)。 */
const POLL_INTERVAL_MS = 2000
const MAX_TRACK_SECONDS = 600

export function usePveTaskTracker() {
  const taskVisible = ref(false)
  const taskTitle = ref('')
  /** running=执行中;ok=成功;error=失败(弹窗展示 exitstatus) */
  const taskPhase = ref<'running' | 'ok' | 'error'>('running')
  const taskDetail = ref('')
  const taskElapsed = ref(0)

  let pollTimer: number | undefined
  let active: { onDone: () => void } | null = null
  let inFlight = false
  let startedAt = 0

  function stopTimers() {
    if (pollTimer) {
      window.clearInterval(pollTimer)
      pollTimer = undefined
    }
  }

  function closeTaskDialog() {
    // 手动关窗只收起 UI;轮询继续,终态后 onSuccess 照常触发
    taskVisible.value = false
  }

  function track(opts: PveTaskTrackOptions) {
    stopTimers()
    taskTitle.value = opts.title
    taskPhase.value = 'running'
    taskDetail.value = ''
    taskElapsed.value = 0
    startedAt = Date.now()
    taskVisible.value = true
    active = {
      onDone: () => opts.onSuccess?.(),
    }

    pollTimer = window.setInterval(async () => {
      if (!active || inFlight) return
      // 已超上限:放弃跟踪,让用户去 PVE 控制台看
      if (Math.round((Date.now() - startedAt) / 1000) >= MAX_TRACK_SECONDS) {
        finish('error', '任务跟踪超时(10 分钟),请到 PVE 管理界面查看任务状态')
        return
      }
      inFlight = true
      try {
        const res = await pveAPI.taskStatus(opts.connId, opts.node, opts.upid)
        if (!active) return
        taskElapsed.value = Math.round((Date.now() - startedAt) / 1000)
        if (res.data.done) {
          if (res.data.ok) {
            finish('ok', '')
          } else {
            finish('error', res.data.exitstatus || '任务失败(PVE 未返回原因)')
          }
        }
      } catch {
        // 网络抖动:下一轮重试,不打断跟踪
      } finally {
        inFlight = false
      }
    }, POLL_INTERVAL_MS)
  }

  function finish(phase: 'ok' | 'error', detail: string) {
    stopTimers()
    if (!active) return
    const onDone = active.onDone
    active = null
    taskPhase.value = phase
    taskDetail.value = detail
    if (phase === 'ok') {
      onDone()
      // 成功后短暂停留再自动关窗,给用户一个明确的「完成」信号
      window.setTimeout(() => {
        if (taskPhase.value === 'ok') taskVisible.value = false
      }, 1600)
    }
  }

  /** 组件卸载时清理(避免定时器泄漏)。 */
  function dispose() {
    stopTimers()
    active = null
  }

  return {
    taskVisible,
    taskTitle,
    taskPhase,
    taskDetail,
    taskElapsed,
    track,
    closeTaskDialog,
    dispose,
  }
}
