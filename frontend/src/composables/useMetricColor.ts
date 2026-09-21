import { useThresholdStore } from '@/stores/threshold'
import { barColor, type ThresholdMetric } from '@/utils/metrics'

/**
 * 组件侧的阈值配色入口,用法与各面板原先自己写的 metricColor(p) 基本一致,
 * 只是多带一个指标名以便查表:metricColor(cpuPct(row), 'cpu')。
 *
 * 首次调用触发一次阈值拉取(store.load 幂等,多个面板同时挂载也只请求一次);
 * 表还没回来时 barColor 自动回退到统一 70/90,阈值到位后组件会因 store.table
 * 的响应式依赖自动重渲染。
 */
export function useMetricColor() {
  const store = useThresholdStore()
  void store.load()
  return (value: number | null | undefined, metric?: ThresholdMetric): string => barColor(value, metric, store.table)
}
