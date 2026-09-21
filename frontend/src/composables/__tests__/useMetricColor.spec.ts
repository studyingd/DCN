import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useMetricColor } from '../useMetricColor'
import { useThresholdStore } from '@/stores/threshold'
import type { MetricThresholdsResponse } from '@/types/inspection'

// 阈值接口全程 mock,不发真实请求;vi.mock 会被提升,工厂里的变量要用 vi.hoisted 一起提升
const { getThresholds } = vi.hoisted(() => ({ getThresholds: vi.fn() }))
vi.mock('@/api', () => ({ inspectionAPI: { getThresholds } }))

const TABLE = {
  cpu: { warning: 70, critical: 85 },
  memory: { warning: 75, critical: 90 },
  disk: { warning: 80, critical: 95 },
}

function okResponse(items: unknown = TABLE): { data: MetricThresholdsResponse } {
  return { data: { items } as MetricThresholdsResponse }
}

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

/** 等 useMetricColor() 里那次 fire-and-forget 的 load 落地(store.load 幂等,会复用同一个 promise) */
async function flushLoad() {
  await useThresholdStore().load()
}

beforeEach(() => {
  setActivePinia(createPinia())
  getThresholds.mockReset()
  getThresholds.mockResolvedValue(okResponse())
})

describe('useMetricColor', () => {
  it('返回一个和原先 metricColor(p) 用法一致的函数', () => {
    const colorOf = useMetricColor()
    expect(typeof colorOf).toBe('function')
  })

  it('调用时触发一次阈值拉取', async () => {
    useMetricColor()
    await flushLoad()
    expect(getThresholds).toHaveBeenCalledTimes(1)
  })

  it('多个面板同时挂载也只请求一次(load 幂等 + store 单例)', async () => {
    useMetricColor()
    useMetricColor()
    const colorOf = useMetricColor()
    await flushLoad()
    expect(getThresholds).toHaveBeenCalledTimes(1)
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-danger)')
  })

  it('阈值表到位后按指标分级,输出的是 tokens.css 里的变量而不是硬编码色值', async () => {
    const colorOf = useMetricColor()
    await flushLoad()
    expect(colorOf(69.9, 'cpu')).toBe('var(--dcn-success)')
    expect(colorOf(70, 'cpu')).toBe('var(--dcn-warning)')
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-danger)')
    expect(colorOf(74.9, 'memory')).toBe('var(--dcn-success)')
    expect(colorOf(94.9, 'disk')).toBe('var(--dcn-warning)')
    expect(colorOf(95, 'disk')).toBe('var(--dcn-danger)')
  })

  it('表还没回来时先用统一 70/90,到位后同一个函数立刻按指标分级', async () => {
    const pending = deferred<ReturnType<typeof okResponse>>()
    getThresholds.mockReturnValue(pending.promise)
    const colorOf = useMetricColor()

    // 首屏:CPU 85 还只能按旧口径判成黄
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-warning)')

    pending.resolve(okResponse())
    await flushLoad()
    // 函数读的是响应式的 store.table,不用重新创建就能拿到新阈值
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-danger)')
  })

  it('请求失败时回退统一 70/90,不抛异常也不白屏', async () => {
    getThresholds.mockRejectedValue(new Error('500'))
    const colorOf = useMetricColor()
    await expect(flushLoad()).resolves.toBeUndefined()
    expect(useThresholdStore().table).toEqual({})
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-warning)')
    expect(colorOf(90, 'cpu')).toBe('var(--dcn-danger)')
    expect(colorOf(10, 'disk')).toBe('var(--dcn-success)')
  })

  it('空值返回中性蓝,不查表', async () => {
    const colorOf = useMetricColor()
    await flushLoad()
    expect(colorOf(null)).toBe('var(--dcn-primary-light)')
    expect(colorOf(undefined)).toBe('var(--dcn-primary-light)')
    expect(colorOf(null, 'cpu')).toBe('var(--dcn-primary-light)')
  })

  it('不传 metric 时即使表已加载也走兜底(调用点忘了标指标不会拿错阈值)', async () => {
    const colorOf = useMetricColor()
    await flushLoad()
    expect(colorOf(85)).toBe('var(--dcn-warning)')
    expect(colorOf(70)).toBe('var(--dcn-warning)')
    expect(colorOf(69.9)).toBe('var(--dcn-success)')
  })

  it('后端只下发了部分指标时,缺的那个指标走兜底', async () => {
    getThresholds.mockResolvedValue(okResponse({ cpu: { warning: 70, critical: 85 } }))
    const colorOf = useMetricColor()
    await flushLoad()
    expect(colorOf(85, 'cpu')).toBe('var(--dcn-danger)')
    // 没有 disk 阈值项,磁盘仍按统一 70/90
    expect(colorOf(85, 'disk')).toBe('var(--dcn-warning)')
    expect(colorOf(90, 'disk')).toBe('var(--dcn-danger)')
  })
})
