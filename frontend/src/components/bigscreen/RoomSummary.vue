<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, LegendComponent } from 'echarts/components'
import type { RoomSummaryItem } from '@/types/dashboard'

use([CanvasRenderer, BarChart, TitleComponent, TooltipComponent, GridComponent, LegendComponent])

const props = defineProps<{
  rooms: RoomSummaryItem[]
}>()

const chartOption = computed(() => {
  const roomNames = props.rooms.map((r) => r.name)
  const onlineData = props.rooms.map((r) => r.device_online)
  const offlineData = props.rooms.map((r) => r.device_offline)

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: '#111827',
      borderColor: '#223047',
      textStyle: { color: '#f1f5f9', fontSize: 12 },
    },
    legend: {
      data: ['在线', '离线'],
      textStyle: { color: '#94a3b8', fontSize: 11 },
      top: 0,
      itemWidth: 10,
      itemHeight: 10,
    },
    grid: {
      left: 80,
      right: 20,
      top: 30,
      bottom: 10,
    },
    xAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: 'rgba(200,215,245,0.15)' } },
      axisLabel: { color: 'rgba(200,215,245,0.5)', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(200,215,245,0.06)' } },
    },
    yAxis: {
      type: 'category',
      data: roomNames,
      axisLine: { lineStyle: { color: 'rgba(200,215,245,0.15)' } },
      axisLabel: { color: 'rgba(200,215,245,0.7)', fontSize: 11 },
    },
    series: [
      {
        name: '在线',
        type: 'bar',
        stack: 'total',
        data: onlineData,
        itemStyle: { color: '#22c55e', borderRadius: [0, 0, 0, 0] },
        barWidth: 14,
      },
      {
        name: '离线',
        type: 'bar',
        stack: 'total',
        data: offlineData,
        itemStyle: { color: '#f05252', borderRadius: [0, 0, 0, 0] },
      },
    ],
  }
})
</script>

<template>
  <div class="room-summary">
    <div class="panel-title">机房概览</div>
    <v-chart class="chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.room-summary {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.chart {
  flex: 1;
  min-height: 160px;
}
</style>
