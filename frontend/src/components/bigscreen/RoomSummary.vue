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
  const maintenanceData = props.rooms.map((r) => r.device_maintenance)

  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(10, 14, 39, 0.9)',
      borderColor: 'rgba(0, 170, 255, 0.3)',
      textStyle: { color: '#e8edf5', fontSize: 12 },
    },
    legend: {
      data: ['在线', '离线', '维护中'],
      textStyle: { color: 'rgba(200,215,245,0.65)', fontSize: 11 },
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
        itemStyle: { color: '#00ff88', borderRadius: [0, 0, 0, 0] },
        barWidth: 14,
      },
      {
        name: '离线',
        type: 'bar',
        stack: 'total',
        data: offlineData,
        itemStyle: { color: '#ff4d6a', borderRadius: [0, 0, 0, 0] },
      },
      {
        name: '维护中',
        type: 'bar',
        stack: 'total',
        data: maintenanceData,
        itemStyle: { color: '#ff9500', borderRadius: [0, 2, 2, 0] },
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
