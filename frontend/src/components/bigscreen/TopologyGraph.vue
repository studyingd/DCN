<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { GraphChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent } from 'echarts/components'
import type { TopologyNode, TopologyEdge } from '@/types/dashboard'

use([CanvasRenderer, GraphChart, TitleComponent, TooltipComponent])

const props = defineProps<{
  nodes: TopologyNode[]
  edges: TopologyEdge[]
}>()

const TYPE_COLORS: Record<string, string> = {
  server: '#5b9eff',
  switch: '#00ccff',
  router: '#00ff88',
  firewall: '#ff9500',
  host: '#a78bfa',
}

const STATUS_BORDER: Record<string, string> = {
  online: '#00ff88',
  offline: '#ff4d6a',
  maintenance: '#ff9500',
}

const CONN_COLORS: Record<string, string> = {
  ethernet: '#5b9eff',
  fiber: '#ff9500',
  serial: '#888888',
}

const chartOption = computed(() => {
  // Compute connection count per node for sizing
  const connCount: Record<number, number> = {}
  for (const edge of props.edges) {
    connCount[edge.source] = (connCount[edge.source] || 0) + 1
    connCount[edge.target] = (connCount[edge.target] || 0) + 1
  }
  const maxConn = Math.max(...Object.values(connCount), 1)

  const graphNodes = props.nodes.map((node) => {
    const size = 20 + ((connCount[node.id] || 0) / maxConn) * 30
    const borderColor = STATUS_BORDER[node.status] || '#5b9eff'
    const fillColor = TYPE_COLORS[node.type] || '#5b9eff'
    return {
      id: String(node.id),
      name: node.name,
      symbolSize: size,
      itemStyle: {
        color: fillColor,
        borderColor,
        borderWidth: 2,
      },
      label: {
        show: true,
        color: '#e8edf5',
        fontSize: 10,
        position: 'bottom',
        distance: 5,
      },
      tooltip: {
        formatter: () => {
          const statusLabel: Record<string, string> = { online: '在线', offline: '离线', maintenance: '维护中' }
          const typeLabel: Record<string, string> = { server: '服务器', switch: '交换机', router: '路由器', firewall: '防火墙', host: '主机' }
          return `<b>${node.name}</b><br/>类型: ${typeLabel[node.type] || node.type}<br/>状态: ${statusLabel[node.status] || node.status}${node.ip ? '<br/>IP: ' + node.ip : ''}${node.room_name ? '<br/>机房: ' + node.room_name : ''}`
        },
      },
    }
  })

  const graphEdges = props.edges.map((edge) => ({
    source: String(edge.source),
    target: String(edge.target),
    lineStyle: {
      color: CONN_COLORS[edge.conn_type || ''] || '#5b9eff88',
      width: 1.5,
      curveness: 0.2,
    },
  }))

  return {
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(10, 14, 39, 0.92)',
      borderColor: 'rgba(0, 170, 255, 0.3)',
      textStyle: { color: '#e8edf5', fontSize: 12 },
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        data: graphNodes,
        links: graphEdges,
        force: {
          repulsion: 300,
          gravity: 0.1,
          edgeLength: 120,
          layoutAnimation: true,
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: { width: 3 },
        },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
      },
    ],
    animationDuration: 1000,
    animationEasingUpdate: 'quinticInOut',
  }
})
</script>

<template>
  <div class="topology-graph">
    <div class="panel-title">网络拓扑</div>
    <v-chart class="chart" :option="chartOption" autoresize />
  </div>
</template>

<style scoped>
.topology-graph {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
  flex: 1;
}

.chart {
  flex: 1;
  min-height: 300px;
}
</style>
