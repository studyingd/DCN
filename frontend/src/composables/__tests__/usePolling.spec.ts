/** @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePolling } from '@/composables/usePolling'

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  setHidden(false)
})

describe('usePolling', () => {
  it('按间隔循环调用 fetch,stop 后停表', async () => {
    const fetcher = vi.fn()
    const poll = usePolling(fetcher, { intervalMs: 5000 })
    poll.start()
    expect(fetcher).not.toHaveBeenCalled() // 首 tick 在间隔之后
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    poll.stop()
    await vi.advanceTimersByTimeAsync(20000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('shouldContinue 返回 false 时自动停表', async () => {
    const fetcher = vi.fn()
    let call = 0
    const poll = usePolling(fetcher, { intervalMs: 1000, shouldContinue: () => call < 1 })
    poll.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    call = 1
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(poll.running.value).toBe(false)
  })

  it('页面隐藏时不发请求,恢复可见后立即补一轮', async () => {
    const fetcher = vi.fn()
    const poll = usePolling(fetcher, { intervalMs: 1000, hiddenMs: 3000 })
    poll.start()
    setHidden(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(3000) // 隐藏期间只重排,不请求
    expect(fetcher).not.toHaveBeenCalled()
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(fetcher).toHaveBeenCalledTimes(1)
    // 补轮之后恢复正常间隔
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    poll.stop()
  })

  it('fetch 抛错即停表(需要继续轮询的调用方应自行 catch 静默)', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('boom'))
    const poll = usePolling(fetcher, { intervalMs: 1000 })
    poll.start()
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(poll.running.value).toBe(false)
  })

  it('重复 start 重置链条,不产生双定时器', async () => {
    const fetcher = vi.fn()
    const poll = usePolling(fetcher, { intervalMs: 1000 })
    poll.start()
    poll.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    poll.stop()
  })
})
