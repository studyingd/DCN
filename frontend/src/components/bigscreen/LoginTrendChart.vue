<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent } from 'echarts/components'
import type { LoginTrendItem } from '@/types/dashboard'

use([CanvasRenderer, LineChart, TitleComponent, TooltipComponent, GridComponent])

const props = defineProps<{
  trend: LoginTrendItem[]
}>()

const chartOption = computed(() => {
  const dates = props.trend.map((item) => {
    const d = new Date(item.date)
    return `${d.getMonth() + 1}/${d.getDate()}`
  })
  const values = props.trend.map((item) => item.login_count)

  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(10, 14, 39, 0.9)',
      borderColor: 'rgba(0, 170, 255, 0.3)',
      textStyle: { color: '#e8edf5', fontSize: 12 },
    },
    grid: {
      left: 36,
      right: 12,
      top: 16,
      bottom: 24,
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLine: { lineStyle: { color: 'rgba(200,215,245,0.15)' } },
      axisLabel: { color: 'rgba(200,215,245,0.5)', fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: { color: 'rgba(200,215,245,0.4)', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(200,215,245,0.06)' } },
    },
    series: [
      {
        type: 'line',
        smooth: true,
        data: values,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { color: '#5b9eff', width: 2 },
        itemStyle: { color: '#5b9eff' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(91, 158, 255, 0.4)' },
              { offset: 1, color: 'rgba(91, 158, 255, 0.02)' },
            ],
          },
        },
      },
    ],
  }
})
</script>

<template>
  <div class="login-trend-chart">
    <div class="panel-title">登录趋势 (近7天)</div>
    <v-chart class="chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.login-trend-chart {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.chart {
  flex: 1;
  min-height: 140px;
}
</style>
