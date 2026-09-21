import { ref } from 'vue'
import { defineStore } from 'pinia'
import { inspectionAPI } from '@/api'
import type { MetricThresholdTable } from '@/types/inspection'

/**
 * 巡检阈值表(cpu / memory / disk 的 warning、critical),由 GET /inspection/thresholds 下发。
 * 指标页、设备详情、PVE、容器页共用这一份,配色口径与后端巡检判定保持一致;
 * 后端调阈值时前端不用跟着发版。
 */
export const useThresholdStore = defineStore('threshold', () => {
  const table = ref<MetricThresholdTable>({})
  // load() 的幂等靠这两个变量:loaded 表示已经拿到过表,inflight 让并发调用共享同一个 promise
  let loaded = false
  let inflight: Promise<void> | null = null

  async function load(): Promise<void> {
    if (loaded || inflight) return inflight ?? Promise.resolve()
    inflight = (async () => {
      try {
        const res = await inspectionAPI.getThresholds()
        table.value = res.data?.items ?? {}
        loaded = true
      } catch {
        // 拉不到阈值不能把页面打白:保持空表,barColor 会回退到统一 70/90,
        // 同时不置 loaded,下次进页面还能重试。
        table.value = {}
      } finally {
        inflight = null
      }
    })()
    return inflight
  }

  return { table, load }
})
