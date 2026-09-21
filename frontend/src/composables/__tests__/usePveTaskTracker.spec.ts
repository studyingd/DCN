import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePveTaskTracker } from '../usePveTaskTracker'
import type { PveTaskStatus } from '@/types/pve'

// taskStatus 全程 mock,不发真实请求
const { taskStatus } = vi.hoisted(() => ({ taskStatus: vi.fn() }))
vi.mock('@/api', () => ({ pveAPI: { taskStatus } }))

function status(data: Partial<PveTaskStatus>): { data: PveTaskStatus } {
  return {
    data: {
      status: 'running',
      exitstatus: null,
      ok: false,
      done: false,
      ...data,
    } as PveTaskStatus,
  }
}

describe('usePveTaskTracker（PVE 异步任务跟踪）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    taskStatus.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  function setup() {
    const tracker = usePveTaskTracker()
    tracker.track({ connId: 1, node: 'pve1', upid: 'UPID:pve1:qmcreate:105:root@pam:1:', title: '创建虚拟机 web-01' })
    return tracker
  }

  it('提交后进入运行态并开始轮询', async () => {
    taskStatus.mockResolvedValue(status({}))
    const t = setup()
    expect(t.taskVisible.value).toBe(true)
    expect(t.taskPhase.value).toBe('running')
    await vi.advanceTimersByTimeAsync(2000)
    expect(taskStatus).toHaveBeenCalledTimes(1)
    expect(taskStatus).toHaveBeenCalledWith(1, 'pve1', 'UPID:pve1:qmcreate:105:root@pam:1:')
    // 未到终态:继续轮询
    await vi.advanceTimersByTimeAsync(4000)
    expect(taskStatus).toHaveBeenCalledTimes(3)
    expect(t.taskPhase.value).toBe('running')
  })

  it('任务成功:触发 onSuccess、自动关窗、停止轮询', async () => {
    const onSuccess = vi.fn()
    const tracker = usePveTaskTracker()
    tracker.track({
      connId: 1,
      node: 'pve1',
      upid: 'UPID:pve1:clone:106:root@pam:1:',
      title: '克隆',
      onSuccess,
    })
    taskStatus.mockResolvedValue(status({ status: 'stopped', done: true, ok: true, exitstatus: 'OK' }))
    await vi.advanceTimersByTimeAsync(2000)
    expect(tracker.taskPhase.value).toBe('ok')
    expect(onSuccess).toHaveBeenCalledTimes(1)
    // 1.6s 后自动关窗
    await vi.advanceTimersByTimeAsync(1700)
    expect(tracker.taskVisible.value).toBe(false)
    // 终态后不再轮询
    const calls = taskStatus.mock.calls.length
    await vi.advanceTimersByTimeAsync(6000)
    expect(taskStatus.mock.calls.length).toBe(calls)
  })

  it('任务失败:停在弹窗展示 PVE 的 exitstatus', async () => {
    taskStatus.mockResolvedValue(
      status({ status: 'stopped', done: true, ok: false, exitstatus: 'ERROR: storage full' }),
    )
    const t = setup()
    await vi.advanceTimersByTimeAsync(2000)
    expect(t.taskPhase.value).toBe('error')
    expect(t.taskDetail.value).toBe('ERROR: storage full')
    expect(t.taskVisible.value).toBe(true) // 失败不自动关窗
  })

  it('网络抖动不中断轮询', async () => {
    taskStatus
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue(status({ status: 'stopped', done: true, ok: true, exitstatus: 'OK' }))
    const t = setup()
    await vi.advanceTimersByTimeAsync(2000)
    expect(t.taskPhase.value).toBe('running') // 失败那一轮不改变状态
    await vi.advanceTimersByTimeAsync(2000)
    expect(t.taskPhase.value).toBe('ok')
  })

  it('dispose 清定时器:卸载后不再轮询', async () => {
    taskStatus.mockResolvedValue(status({}))
    const t = setup()
    t.dispose()
    await vi.advanceTimersByTimeAsync(10000)
    expect(taskStatus).not.toHaveBeenCalled()
  })

  it('超过 10 分钟上限:标记超时并停止跟踪', async () => {
    taskStatus.mockResolvedValue(status({}))
    const t = setup()
    await vi.advanceTimersByTimeAsync(600 * 1000 + 2000)
    expect(t.taskPhase.value).toBe('error')
    expect(t.taskDetail.value).toContain('超时')
  })

  it('上一轮未结束时再 track:新任务替换旧任务', async () => {
    taskStatus.mockResolvedValue(status({}))
    const tracker = usePveTaskTracker()
    tracker.track({ connId: 1, node: 'pve1', upid: 'UPID:A', title: '任务A' })
    tracker.track({ connId: 1, node: 'pve1', upid: 'UPID:B', title: '任务B' })
    expect(tracker.taskTitle.value).toBe('任务B')
    await vi.advanceTimersByTimeAsync(2000)
    expect(taskStatus).toHaveBeenCalledWith(1, 'pve1', 'UPID:B')
    expect(taskStatus).not.toHaveBeenCalledWith(1, 'pve1', 'UPID:A')
  })
})
