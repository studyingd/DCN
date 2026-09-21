<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { GaugeChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent } from 'echarts/components'

use([CanvasRenderer, GaugeChart, TitleComponent, TooltipComponent])

const props = defineProps<{
  deviceOnline: number
  deviceTotal: number
}>()

const rate = computed(() => {
  if (props.deviceTotal === 0) return 0
  return parseFloat(((props.deviceOnline / props.deviceTotal) * 100).toFixed(1))
})

const gaugeColor = computed(() => {
  if (rate.value >= 90) return '#22c55e'
  if (rate.value >= 70) return '#d97706'
  return '#f05252'
})

const chartOption = computed(() => ({
  series: [
    {
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max: 100,
      radius: '85%',
      center: ['50%', '55%'],
      splitNumber: 10,
      axisLine: {
        lineStyle: {
          width: 12,
          color: [
            [0.7, '#f05252'],
            [0.9, '#d97706'],
            [1, '#22c55e'],
          ],
        },
      },
      pointer: {
        length: '60%',
        width: 4,
        itemStyle: { color: gaugeColor.value },
      },
      axisTick: {
        distance: -12,
        length: 4,
        lineStyle: { color: 'rgba(200,215,245,0.3)', width: 1 },
      },
      splitLine: {
        distance: -14,
        length: 8,
        lineStyle: { color: 'rgba(200,215,245,0.4)', width: 1 },
      },
      axisLabel: {
        distance: 16,
        color: 'rgba(200,215,245,0.4)',
        fontSize: 9,
      },
      detail: {
        valueAnimation: true,
        formatter: `{value}%`,
        color: gaugeColor.value,
        fontSize: 22,
        fontWeight: 'bold',
        offsetCenter: [0, '70%'],
        fontFamily: 'Courier New',
      },
      title: {
        show: true,
        offsetCenter: [0, '92%'],
        color: 'rgba(200,215,245,0.5)',
        fontSize: 12,
      },
      data: [{ value: rate.value, name: '在线率' }],
    },
  ],
}))
</script>

<template>
  <div class="online-rate-gauge">
    <div class="panel-title">在线率</div>
    <v-chart class="chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.online-rate-gauge {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.chart {
  flex: 1;
  min-height: 160px;
}
</style>
