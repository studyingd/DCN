import { ref } from 'vue'

export interface UsePollingOptions {
  /** 轮询间隔(ms)。首 tick 在间隔之后,初始加载由调用方自己完成 */
  intervalMs: number
  /** 每个 tick 前调用;返回 false 时停表(如「没有在跑的任务」) */
  shouldContinue?: () => boolean
  /** 页面隐藏期间的等待间隔,默认同 intervalMs。隐藏时不发请求,恢复可见后立即补一轮 */
  hiddenMs?: number
}

/**
 * 轻量轮询(setTimeout 链,非 setInterval),统一替换此前四处各写一套的定时器:
 * 任务列表 5s / 任务详情 1.5s / Agent run 3s。
 * 口径:页面隐藏期间不发请求,恢复可见后立即补一轮(旧实现里详情抽屉隐藏
 * 3s 后照发、run 轮询完全不感知 hidden,统一收敛到这里)。
 * fetch 抛错即停表:需要「失败继续轮询」的调用方在自己的 fetch 里 try/catch 静默。
 * 不挂 onUnmounted:组件里自行在 onUnmounted 里 stop,也方便单测直接调用。
 */
export function usePolling(fetch: () => Promise<void> | void, options: UsePollingOptions) {
  const { intervalMs, shouldContinue, hiddenMs = intervalMs } = options
  const running = ref(false)
  let timer: number | undefined
  let skippedWhileHidden = false

  function clearTimer() {
    if (timer != null) {
      window.clearTimeout(timer)
      timer = undefined
    }
  }

  async function tick() {
    timer = undefined
    if (!running.value) return
    if (shouldContinue && !shouldContinue()) {
      stop()
      return
    }
    if (document.hidden) {
      skippedWhileHidden = true
      timer = window.setTimeout(tick, hiddenMs)
      return
    }
    skippedWhileHidden = false
    try {
      await fetch()
    } catch {
      // fetch 未自行消化的异常:停表,避免未处理拒绝在定时器链里反复炸
      stop()
      return
    }
    // fetch 期间可能被 stop() 或 start()(重启链),不要覆盖新排的定时器
    if (running.value && timer == null) timer = window.setTimeout(tick, intervalMs)
  }

  function onVisible() {
    if (document.hidden || !running.value || timer == null || !skippedWhileHidden) return
    clearTimer()
    void tick()
  }

  function start() {
    stop()
    running.value = true
    skippedWhileHidden = false
    document.addEventListener('visibilitychange', onVisible)
    timer = window.setTimeout(tick, intervalMs)
  }

  function stop() {
    running.value = false
    clearTimer()
    document.removeEventListener('visibilitychange', onVisible)
  }

  return { start, stop, running }
}
