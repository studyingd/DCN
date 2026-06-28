<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GraphicComponent } from 'echarts/components'
import type { DeviceTypeItem } from '@/types/dashboard'

use([CanvasRenderer, PieChart, TitleComponent, TooltipComponent, LegendComponent, GraphicComponent])

const props = defineProps<{
  distribution: DeviceTypeItem[]
}>()

const TYPE_COLORS: Record<string, string> = {
  server: '#5b9eff',
  switch: '#00ccff',
  router: '#00ff88',
  firewall: '#ff9500',
  host: '#a78bfa',
}

const chartOption = computed(() => {
  const data = props.distribution.map((item) => ({
    name: item.label || item.type,
    value: item.count,
    itemStyle: {
      color: TYPE_COLORS[item.type] || '#5b9eff',
    },
  }))

  const total = data.reduce((s, d) => s + d.value, 0)

  return {
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(10, 14, 39, 0.9)',
      borderColor: 'rgba(0, 170, 255, 0.3)',
      textStyle: { color: '#e8edf5', fontSize: 13 },
      formatter: (params: { name: string; value: number; percent: number }) => {
        return `${params.name}<br/>数量: <b>${params.value}</b><br/>占比: ${params.percent}%`
      },
    },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center',
      textStyle: { color: 'rgba(200, 215, 245, 0.7)', fontSize: 11 },
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 8,
    },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['35%', '50%'],
        avoidLabelOverlap: false,
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 'bold', color: '#e8edf5' },
        },
        labelLine: { show: false },
        data,
      },
    ],
    graphic: [
      {
        type: 'text',
        left: '30%',
        top: '43%',
        style: {
          text: String(total),
          textAlign: 'center',
          fill: '#e8edf5',
          fontSize: 24,
          fontWeight: 'bold',
          fontFamily: 'Courier New',
        },
      },
      {
        type: 'text',
        left: '30%',
        top: '55%',
        style: {
          text: '设备总计',
          textAlign: 'center',
          fill: 'rgba(200, 215, 245, 0.5)',
          fontSize: 11,
        },
      },
    ],
  }
})
</script>

<template>
  <div class="device-type-chart">
    <div class="panel-title">设备类型分布</div>
    <v-chart class="chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.device-type-chart {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.chart {
  flex: 1;
  min-height: 180px;
}
</style>
