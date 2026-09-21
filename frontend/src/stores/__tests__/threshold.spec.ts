import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useThresholdStore } from '../threshold'
import type { MetricThresholdsResponse } from '@/types/inspection'

// vi.mock 会被提升到所有 import 之前执行,工厂里不能引用普通顶层变量,所以用 vi.hoisted 一起提升。
// 阈值接口全程走 mock,不发真实请求。
const { getThresholds } = vi.hoisted(() => ({ getThresholds: vi.fn() }))
vi.mock('@/api', () => ({ inspectionAPI: { getThresholds } }))

const TABLE = {
  cpu: { warning: 70, critical: 85 },
  memory: { warning: 75, critical: 90 },
  disk: { warning: 80, critical: 95 },
  failed_services: { warning: 1, critical: 3 },
}

function okResponse(items: unknown = TABLE): { data: MetricThresholdsResponse } {
  return { data: { items } as MetricThresholdsResponse }
}

/** 手动控制 resolve 时机,用来验证并发调用只发一次请求 */
function deferred<T>() {
  let resolveFn: ((value: T) => void) | undefined
  const promise = new Promise<T>((resolve) => {
    resolveFn = resolve
  })
  return {
    promise,
    resolve(value: T) {
      resolveFn?.(value)
    },
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  getThresholds.mockReset()
})

describe('useThresholdStore', () => {
  it('初始是空表,此时 barColor 会走统一 70/90 兜底', () => {
    expect(useThresholdStore().table).toEqual({})
    expect(getThresholds).not.toHaveBeenCalled()
  })

  it('load() 拉取阈值表并写入 table', async () => {
    getThresholds.mockResolvedValue(okResponse())
    const store = useThresholdStore()
    await store.load()
    expect(getThresholds).toHaveBeenCalledTimes(1)
    expect(store.table).toEqual(TABLE)
  })

  it('load() 幂等:已经加载过就不再发请求', async () => {
    getThresholds.mockResolvedValue(okResponse())
    const store = useThresholdStore()
    await store.load()
    await store.load()
    await store.load()
    expect(getThresholds).toHaveBeenCalledTimes(1)
  })

  it('并发 load() 共享同一个 promise,只发一次请求', async () => {
    const pending = deferred<ReturnType<typeof okResponse>>()
    getThresholds.mockReturnValue(pending.promise)
    const store = useThresholdStore()

    const first = store.load()
    const second = store.load()
    const third = store.load()
    expect(getThresholds).toHaveBeenCalledTimes(1)

    pending.resolve(okResponse())
    await Promise.all([first, second, third])
    expect(store.table).toEqual(TABLE)
    expect(getThresholds).toHaveBeenCalledTimes(1)
  })

  it('多个面板各自挂载也共用同一个 store 实例,合计只请求一次', async () => {
    getThresholds.mockResolvedValue(okResponse())
    await useThresholdStore().load()
    await useThresholdStore().load()
    expect(getThresholds).toHaveBeenCalledTimes(1)
  })

  it('请求失败时 table 保持空对象,且不把异常抛给调用方', async () => {
    getThresholds.mockRejectedValue(new Error('500'))
    const store = useThresholdStore()
    await expect(store.load()).resolves.toBeUndefined()
    expect(store.table).toEqual({})
  })

  it('失败后允许重试,重试成功就写入表', async () => {
    const store = useThresholdStore()
    getThresholds.mockRejectedValueOnce(new Error('network'))
    await store.load()
    expect(store.table).toEqual({})

    getThresholds.mockResolvedValueOnce(okResponse())
    await store.load()
    expect(getThresholds).toHaveBeenCalledTimes(2)
    expect(store.table).toEqual(TABLE)
  })

  it('失败之后再次并发调用也只发一次重试请求', async () => {
    const store = useThresholdStore()
    getThresholds.mockRejectedValueOnce(new Error('network'))
    await store.load()

    const pending = deferred<ReturnType<typeof okResponse>>()
    getThresholds.mockReturnValue(pending.promise)
    const retryA = store.load()
    const retryB = store.load()
    expect(getThresholds).toHaveBeenCalledTimes(2)
    pending.resolve(okResponse())
    await Promise.all([retryA, retryB])
    expect(store.table).toEqual(TABLE)
  })

  it('响应缺 items 时兜底成空表,并算作已加载(不反复重试)', async () => {
    getThresholds.mockResolvedValue({ data: {} })
    const store = useThresholdStore()
    await store.load()
    await store.load()
    expect(store.table).toEqual({})
    expect(getThresholds).toHaveBeenCalledTimes(1)
  })

  it('后端新增指标项时原样保留,前端不需要跟着改类型', async () => {
    getThresholds.mockResolvedValue(okResponse({ ...TABLE, load: { warning: 2, critical: 5 } }))
    const store = useThresholdStore()
    await store.load()
    expect(store.table.load).toEqual({ warning: 2, critical: 5 })
  })
})
