<script setup lang="ts">
import { computed, ref } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import type { PveRrdPoint } from '@/types/pve'

use([CanvasRenderer, LineChart, AriaComponent, GridComponent, LegendComponent, TooltipComponent])

const props = defineProps<{ points: PveRrdPoint[] }>()
const metric = ref<'resource' | 'disk' | 'network'>('resource')

function pct(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? Number((n * 100).toFixed(2)) : null
}

function memoryPct(point: PveRrdPoint): number | null {
  const mem = Number(point.mem)
  const max = Number(point.maxmem)
  return Number.isFinite(mem) && max > 0 ? Number(((mem / max) * 100).toFixed(2)) : null
}

function bytes(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function formatRate(value: number): string {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB/s`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB/s`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB/s`
  return `${Math.round(value)} B/s`
}

const validPoints = computed(() => props.points.filter((point) => Number.isFinite(Number(point.time))))

const chartOption = computed(() => {
  const resource = metric.value === 'resource'
  const series = resource
    ? [
        { name: 'CPU', data: validPoints.value.map((p) => [Number(p.time) * 1000, pct(p.cpu)]), color: '#60a5fa' },
        { name: '内存', data: validPoints.value.map((p) => [Number(p.time) * 1000, memoryPct(p)]), color: '#a78bfa' },
      ]
    : metric.value === 'disk'
      ? [
          {
            name: '读取',
            data: validPoints.value.map((p) => [Number(p.time) * 1000, bytes(p.diskread)]),
            color: '#38bdf8',
          },
          {
            name: '写入',
            data: validPoints.value.map((p) => [Number(p.time) * 1000, bytes(p.diskwrite)]),
            color: '#d97706',
          },
        ]
      : [
          {
            name: '接收',
            data: validPoints.value.map((p) => [Number(p.time) * 1000, bytes(p.netin)]),
            color: '#22c55e',
          },
          {
            name: '发送',
            data: validPoints.value.map((p) => [Number(p.time) * 1000, bytes(p.netout)]),
            color: '#60a5fa',
          },
        ]

  return {
    animation: false,
    aria: { enabled: true, decal: { show: true } },
    color: series.map((item) => item.color),
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111827',
      borderColor: '#223047',
      textStyle: { color: '#f1f5f9', fontSize: 12 },
      valueFormatter: (value: number) => (resource ? `${Number(value).toFixed(2)}%` : formatRate(Number(value))),
    },
    legend: { top: 0, right: 4, textStyle: { color: '#94a3b8', fontSize: 11 } },
    grid: { left: 54, right: 16, top: 34, bottom: 32 },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#223047' } },
      axisLabel: { color: '#64748b', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: resource ? 100 : undefined,
      axisLabel: {
        color: '#64748b',
        fontSize: 10,
        formatter: (value: number) => (resource ? `${value}%` : formatRate(value)),
      },
      splitLine: { lineStyle: { color: '#18243a' } },
    },
    series: series.map((item, index) => ({
      name: item.name,
      type: 'line',
      data: item.data,
      showSymbol: false,
      connectNulls: false,
      smooth: 0.2,
      lineStyle: { width: 1.8, type: index === 1 ? 'dashed' : 'solid' },
      symbol: metric.value === 'resource' && item.name === '内存' ? 'diamond' : 'circle',
      areaStyle: { opacity: 0.06 },
      emphasis: { focus: 'series' },
    })),
  }
})
</script>

<template>
  <div class="pve-history-chart">
    <div class="history-metric-switch" aria-label="历史指标类型">
      <button
        v-for="item in [
          ['resource', 'CPU / 内存'],
          ['disk', '磁盘 IO'],
          ['network', '网络流量'],
        ] as const"
        :key="item[0]"
        type="button"
        :class="{ active: metric === item[0] }"
        @click="metric = item[0]"
      >
        {{ item[1] }}
      </button>
    </div>
    <v-chart class="history-chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.pve-history-chart {
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
  overflow: hidden;
}
.history-metric-switch {
  display: flex;
  gap: var(--dcn-space-1);
  padding: var(--dcn-space-2);
  border-bottom: 1px solid var(--dcn-border-light);
}
.history-metric-switch button {
  min-height: 30px;
  padding: 4px 10px;
  border: 1px solid transparent;
  border-radius: var(--dcn-radius-md);
  background: transparent;
  color: var(--dcn-text-secondary);
  font: inherit;
  font-size: var(--dcn-text-xs);
  cursor: pointer;
}
.history-metric-switch button:hover {
  color: var(--dcn-text-primary);
  background: var(--dcn-bg-muted);
}
.history-metric-switch button.active {
  border-color: color-mix(in srgb, var(--dcn-primary) 45%, var(--dcn-border));
  background: var(--dcn-primary-bg);
  color: var(--dcn-primary-light);
}
.history-metric-switch button:focus-visible {
  outline: 2px solid var(--dcn-primary-light);
  outline-offset: 2px;
}
.history-chart {
  width: 100%;
  height: 260px;
}

@media (max-width: 600px) {
  .history-metric-switch {
    flex-wrap: wrap;
  }
  .history-chart {
    height: 230px;
  }
}
</style>
