<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">服务器监控</h2>
      <div class="metrics-toolbar">
        <span class="metrics-subtitle">Linux 走 SSH · Windows 走 WinRM · 每 30s 自动刷新</span>
        <el-select v-model="roomFilter" placeholder="全部机房" clearable class="metrics-filter">
          <el-option v-for="r in roomStore.rooms" :key="r.id" :label="r.name" :value="r.id" />
        </el-select>
        <el-select v-model="rackFilter" placeholder="全部机柜" clearable :disabled="!roomFilter" class="metrics-filter">
          <el-option v-for="rk in rackOptions" :key="rk.id" :label="rk.name" :value="rk.id" />
        </el-select>
        <el-input v-model="keyword" placeholder="搜索名称 / IP" prefix-icon="Search" clearable class="metrics-search" />
        <el-button :icon="Refresh" circle :loading="loading" aria-label="刷新服务器监控数据" @click="load(true)" />
      </div>
    </div>
    <el-alert v-if="pollError" :title="pollError" type="warning" show-icon :closable="false" class="poll-alert" />

    <div class="inline-panel-body metrics-body">
      <el-table
        v-loading="loading && items.length === 0"
        :data="filteredItems"
        row-key="device_id"
        stripe
        class="metrics-table"
        :default-sort="{ prop: 'cpu_pct', order: 'descending' }"
        @row-click="openDetail"
      >
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tooltip :content="statusText(row)" placement="right">
              <span class="status-dot" :class="statusDotClass(row)" />
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column prop="device_name" label="名称" min-width="140" sortable />
        <el-table-column prop="ip_address" label="IP 地址" min-width="120" show-overflow-tooltip />
        <el-table-column prop="os_system" label="操作系统" min-width="190" class-name="os-cell">
          <template #default="{ row }">
            <span>{{ row.os_system || '-' }}</span>
          </template>
        </el-table-column>

        <el-table-column
          label="CPU"
          min-width="150"
          sortable
          :sort-by="sortVal('cpu_pct')"
          class-name="metric-static-column"
        >
          <template #default="{ row }">
            <div class="metric-static" @click.stop @dblclick.stop>
              <MetricBar :value="row.cpu_pct" :available="row.available" :error="row.error" metric="cpu" />
            </div>
          </template>
        </el-table-column>

        <el-table-column
          label="内存"
          min-width="150"
          sortable
          :sort-by="sortVal('mem_pct')"
          class-name="metric-static-column"
        >
          <template #default="{ row }">
            <div class="metric-static" @click.stop @dblclick.stop>
              <MetricBar :value="row.mem_pct" :available="row.available" :error="row.error" metric="memory" />
            </div>
          </template>
        </el-table-column>

        <el-table-column
          label="磁盘(最高分区)"
          min-width="150"
          sortable
          :sort-by="sortVal('disk_max_pct')"
          class-name="metric-static-column"
        >
          <template #default="{ row }">
            <div class="metric-static" @click.stop @dblclick.stop>
              <MetricBar :value="row.disk_max_pct" :available="row.available" :error="row.error" metric="disk" />
            </div>
          </template>
        </el-table-column>

        <el-table-column label="负载" width="80" align="right">
          <template #default="{ row }">
            <span v-if="row.available && row.load1 != null">{{ row.load1.toFixed(2) }}</span>
            <span v-else class="metric-na">-</span>
          </template>
        </el-table-column>

        <el-table-column label="网络 ↓/↑" min-width="150" align="right">
          <template #default="{ row }">
            <span v-if="row.available" class="net-text">
              {{ fmtBps(row.net_rx_bps) }} / {{ fmtBps(row.net_tx_bps) }}
            </span>
            <span v-else class="metric-na">-</span>
          </template>
        </el-table-column>

        <el-table-column label="运行时间" min-width="110" align="right">
          <template #default="{ row }">
            <span v-if="row.available">{{ fmtUptime(row.uptime_sec) }}</span>
            <span v-else class="metric-na">-</span>
          </template>
        </el-table-column>

        <el-table-column label="采集时间" width="95" align="right">
          <template #default="{ row }">
            <span class="fetched-text" :class="{ 'is-stale': isStale(row.fetched_at) }">
              {{ fmtClock(row.fetched_at) }}
              <small v-if="isStale(row.fetched_at)">过期</small>
            </span>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无服务器设备(类型为 server/host 的设备会出现在这里)" />
        </template>
      </el-table>
    </div>

    <!-- 详情抽屉:实时指标 + 分区明细 + 历史曲线 -->
    <el-drawer v-model="drawerVisible" :title="detail?.device_name || '设备详情'" size="660px" destroy-on-close>
      <div v-loading="detailLoading" class="drawer-body">
        <template v-if="detail">
          <el-alert
            v-if="!detail.available"
            :title="detail.error || '指标采集不可用'"
            type="warning"
            :closable="false"
            show-icon
            class="drawer-alert"
          />

          <el-descriptions :column="2" border size="small" class="drawer-desc">
            <el-descriptions-item label="IP 地址">{{ detail.ip_address || '-' }}</el-descriptions-item>
            <el-descriptions-item label="采集通道">
              {{ detail.source === 'winrm' ? 'WinRM' : detail.source === 'ssh' ? 'SSH' : '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="操作系统" :span="2">{{ detail.os_system || '-' }}</el-descriptions-item>
            <el-descriptions-item label="内存">
              <span v-if="detail.mem_total_mb != null">
                {{ fmtMb(detail.mem_used_mb) }} / {{ fmtMb(detail.mem_total_mb) }} ({{ detail.mem_pct }}%)
              </span>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item label="负载 1/5/15">
              <span v-if="detail.load1 != null">
                {{ detail.load1.toFixed(2) }} / {{ detail.load5?.toFixed(2) }} / {{ detail.load15?.toFixed(2) }}
              </span>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item label="运行时间">{{ fmtUptime(detail.uptime_sec) }}</el-descriptions-item>
            <el-descriptions-item label="网络 ↓/↑">
              {{ fmtBps(detail.net_rx_bps) }} / {{ fmtBps(detail.net_tx_bps) }}
            </el-descriptions-item>
            <el-descriptions-item label="磁盘 IO 读/写">
              {{ fmtBps(detail.disk_read_bps) }} / {{ fmtBps(detail.disk_write_bps) }}
            </el-descriptions-item>
          </el-descriptions>

          <template v-if="detail.disks.length > 0">
            <div class="drawer-section-title">磁盘分区</div>
            <el-table :data="detail.disks" size="small" border class="disk-table">
              <el-table-column prop="mount" label="挂载点" min-width="110" />
              <el-table-column label="已用 / 总量" min-width="150">
                <template #default="{ row }">
                  {{ fmtBytes(row.used_bytes) }} / {{ fmtBytes(row.size_bytes) }}
                </template>
              </el-table-column>
              <el-table-column label="使用率" min-width="140">
                <template #default="{ row }">
                  <el-progress :percentage="row.pct" :color="metricColor(row.pct, 'disk')" :stroke-width="10" />
                </template>
              </el-table-column>
            </el-table>
          </template>

          <div class="drawer-section-title history-header">
            <span>历史曲线</span>
            <el-radio-group v-model="historyRange" size="small">
              <el-radio-button value="1h">1小时</el-radio-button>
              <el-radio-button value="6h">6小时</el-radio-button>
              <el-radio-button value="24h">24小时</el-radio-button>
              <el-radio-button value="7d">7天</el-radio-button>
            </el-radio-group>
          </div>
          <div v-loading="historyLoading" class="history-chart">
            <v-chart v-if="historyPoints.length > 0" :option="chartOption" autoresize class="chart" />
            <el-empty
              v-else-if="!historyLoading"
              description="该时间范围内暂无历史数据(采集满一个周期后开始有数据)"
              :image-size="60"
            />
          </div>

          <div class="drawer-section-title">磁盘 IO</div>
          <div v-loading="historyLoading" class="io-chart">
            <v-chart v-if="historyPoints.length > 0" :option="ioChartOption" autoresize class="chart" />
            <el-empty v-else-if="!historyLoading" description="暂无磁盘 IO 历史数据" :image-size="60" />
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, onUnmounted, ref, watch, type PropType } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElProgress } from 'element-plus'
// ElProgress 在 script 里局部导入并经 h() 渲染，模板中的 <el-progress> 会优先绑定到该
// 局部变量，unplugin-vue-components 因此不会自动注入它的样式；进度条样式是否生效就会
// 取决于其它异步面板有没有顺带加载过这份 CSS（表现为刷新后样式不一致）。这里显式引入。
import 'element-plus/es/components/progress/style/css'
import { Refresh } from '@element-plus/icons-vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { metricsAPI } from '@/api'
import { useRoomStore } from '@/stores/room'
import { useMetricColor } from '@/composables/useMetricColor'
import { fmtBps, fmtBytes, fmtClock, fmtMb, fmtPointTs, fmtUptime, type ThresholdMetric } from '@/utils/metrics'
import type { MetricSamplePoint, MetricsDeviceDetail, MetricsDeviceItem, MetricsRange } from '@/types/metrics'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent])

// 抽屉里的磁盘分区表直接用这个;表格单元格的进度条由 MetricBar 自己持有
const metricColor = useMetricColor()

// ── 表格单元格里的阈值配色进度条(采集失败时显示原因) ──
const MetricBar = defineComponent({
  props: {
    value: { type: Number, default: null },
    available: { type: Boolean, default: false },
    error: { type: String, default: null },
    // 指标名决定查哪一组阈值(cpu / memory / disk);不传则走统一兜底
    metric: { type: String as PropType<ThresholdMetric>, default: undefined },
  },
  setup(props) {
    const colorOf = useMetricColor()
    return () => {
      if (!props.available) {
        return h('span', { class: 'metric-na metric-err' }, props.error || '不可用')
      }
      if (props.value == null) {
        return h('span', { class: 'metric-na' }, '-')
      }
      return h(ElProgress, {
        percentage: props.value,
        color: colorOf(props.value, props.metric),
        strokeWidth: 10,
      })
    }
  },
})

// ── 列表数据 + 30s 轮询 ──
const items = ref<MetricsDeviceItem[]>([])
const loading = ref(false)
const pollError = ref('')
const keyword = ref('')
const roomStore = useRoomStore()
const roomFilter = ref<number | null>(null)
const rackFilter = ref<number | null>(null)
let timer: number | undefined
let inFlight = false

// 从 U 位图详情面板跳入(?panel=metrics&device=13):首载后自动打开该设备抽屉
const route = useRoute()
const pendingDeviceId = ref<number | null>(route.query.device ? Number(route.query.device) : null)

if (roomStore.rooms.length === 0) {
  roomStore.fetchRooms()
}

// 选中机房后,机柜下拉只列该机房的机柜
const rackOptions = computed(() => {
  if (!roomFilter.value) return []
  const room = roomStore.rooms.find((r) => r.id === roomFilter.value)
  return room?.racks ?? []
})

watch(roomFilter, () => {
  rackFilter.value = null
})

async function load(manual = false) {
  if (inFlight) return
  inFlight = true
  loading.value = true
  try {
    if (manual) {
      // 手动刷新=先触发一轮真实采集(SSH/WinRM,耗时可达一个采集周期),
      // 再重读列表;周期轮询不触发采集,只读缓存
      try {
        await metricsAPI.collect()
      } catch {
        // 采集失败不阻断列表刷新——列表仍展示上一轮缓存数据
      }
    }
    const res = await metricsAPI.list()
    items.value = res.data.items
    pollError.value = ''
    // 携带 ?device= 跳入时,列表就绪后自动打开对应设备的详情抽屉
    if (pendingDeviceId.value != null) {
      const row = items.value.find((i) => i.device_id === pendingDeviceId.value)
      pendingDeviceId.value = null
      if (row) openDetail(row)
    }
  } catch {
    pollError.value = '服务器指标刷新失败，当前仍显示上一次成功数据，请手动重试'
    if (manual) ElMessage.error('加载服务器指标失败')
  } finally {
    inFlight = false
    loading.value = false
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(() => {
    if (!document.hidden) void load()
  }, 30000)
})
onUnmounted(() => window.clearInterval(timer))

const filteredItems = computed(() => {
  let list = items.value
  if (roomFilter.value) {
    list = list.filter((i) => i.room_id === roomFilter.value)
  }
  if (rackFilter.value) {
    list = list.filter((i) => i.rack_id === rackFilter.value)
  }
  const q = keyword.value.trim().toLowerCase()
  if (!q) return list
  return list.filter((i) => i.device_name.toLowerCase().includes(q) || (i.ip_address || '').toLowerCase().includes(q))
})

function sortVal(key: 'cpu_pct' | 'mem_pct' | 'disk_max_pct') {
  return (row: MetricsDeviceItem) => row[key] ?? -1
}

function statusDotClass(row: MetricsDeviceItem): string {
  const s = (row.status || '').toLowerCase()
  if (s === 'online') return row.available ? 'dot-online' : 'dot-degraded'
  if (s === 'offline') return 'dot-offline'
  return 'dot-offline'
}

function statusText(row: MetricsDeviceItem): string {
  const s = (row.status || '').toLowerCase()
  if (s === 'online') return row.available ? '在线' : `在线,但采集失败:${row.error || '未知原因'}`
  if (s === 'offline') return '离线'
  return '离线'
}
function isStale(ts: string | null, maxAgeSeconds = 120): boolean {
  return !!ts && Date.now() - new Date(ts).getTime() > maxAgeSeconds * 1000
}

// ── 详情抽屉 ──
const drawerVisible = ref(false)
const detail = ref<MetricsDeviceDetail | null>(null)
const detailLoading = ref(false)
const historyPoints = ref<MetricSamplePoint[]>([])
const historyRange = ref<MetricsRange>('1h')
const historyLoading = ref(false)
let currentDeviceId: number | null = null

async function openDetail(row: MetricsDeviceItem) {
  currentDeviceId = row.device_id
  drawerVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const res = await metricsAPI.detail(row.device_id)
    detail.value = res.data
  } catch {
    ElMessage.error('加载设备详情失败')
  } finally {
    detailLoading.value = false
  }
  loadHistory()
}

async function loadHistory() {
  if (currentDeviceId == null) return
  historyLoading.value = true
  try {
    const res = await metricsAPI.history(currentDeviceId, historyRange.value)
    historyPoints.value = res.data.points
  } catch {
    historyPoints.value = []
  } finally {
    historyLoading.value = false
  }
}

watch(historyRange, () => loadHistory())

// ── 历史曲线 ──
const chartOption = computed(() => {
  const xs = historyPoints.value.map((p) => fmtPointTs(p.ts, historyRange.value === '7d'))
  const series = (key: 'cpu_pct' | 'mem_pct' | 'disk_max_pct', name: string, color: string) => ({
    name,
    type: 'line' as const,
    smooth: true,
    showSymbol: false,
    connectNulls: true,
    data: historyPoints.value.map((p) => p[key]),
    lineStyle: { width: 2, color },
    itemStyle: { color },
    areaStyle: { opacity: 0.06 },
  })
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => (v == null ? '-' : `${v}%`) },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 44, right: 16, top: 30, bottom: 24 },
    xAxis: { type: 'category' as const, data: xs, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value' as const, min: 0, max: 100, name: '%' },
    series: [
      series('cpu_pct', 'CPU', '#3b82f6'),
      series('mem_pct', '内存', '#a78bfa'),
      series('disk_max_pct', '磁盘', '#00cc88'),
    ],
  }
})

// ── 磁盘 IO 曲线(与主曲线共享 historyPoints/historyRange) ──
const ioChartOption = computed(() => {
  const xs = historyPoints.value.map((p) => fmtPointTs(p.ts, historyRange.value === '7d'))
  const mk = (key: 'disk_read_bps' | 'disk_write_bps', name: string, color: string) => ({
    name,
    type: 'line' as const,
    smooth: true,
    showSymbol: false,
    connectNulls: true,
    data: historyPoints.value.map((p) => p[key]),
    lineStyle: { width: 2, color },
    itemStyle: { color },
    areaStyle: { opacity: 0.06 },
  })
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: unknown) => (typeof v === 'number' ? fmtBps(v) : '-'),
    },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 70, right: 16, top: 30, bottom: 24 },
    xAxis: { type: 'category' as const, data: xs, axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'value' as const,
      axisLabel: { formatter: (v: number) => fmtBps(v), fontSize: 10 },
    },
    series: [mk('disk_read_bps', '读取', '#38bdf8'), mk('disk_write_bps', '写入', '#f05252')],
  }
})

// ── 格式化 helpers 统一来自 @/utils/metrics ──
</script>

<style scoped>
.metrics-toolbar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.metrics-subtitle {
  font-size: 13px;
  color: var(--dcn-text-secondary);
  white-space: nowrap;
}

.metrics-search {
  width: 220px;
}

.metrics-filter {
  width: 140px;
}

.metrics-body {
  padding: 0;
}

.metrics-table {
  width: 100%;
}

.metrics-table :deep(.el-table__row) {
  cursor: pointer;
}

/* 操作系统列：单行显示，溢出省略号截断，但不弹 tooltip（配合未设置 show-overflow-tooltip）。 */
.metrics-table :deep(td.os-cell .cell) {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metrics-table :deep(td.metric-static-column),
.metric-static {
  cursor: default;
}

.status-dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
}

.dot-online {
  background: var(--dcn-success);
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.45);
}

.dot-degraded {
  background: var(--dcn-warning);
  box-shadow: 0 0 6px rgba(217, 119, 6, 0.45);
}

.dot-offline {
  background: var(--dcn-danger);
}

.dot-maintenance {
  background: var(--dcn-info);
}

.metric-na {
  color: var(--dcn-text-placeholder);
  font-size: 12px;
}

.metric-err {
  display: inline-block;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
  color: var(--dcn-warning);
}

.net-text {
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}

.fetched-text {
  color: var(--dcn-text-placeholder);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.drawer-body {
  padding: 0 var(--dcn-space-2);
}

.drawer-alert {
  margin-bottom: var(--dcn-space-3);
}

.drawer-desc {
  margin-bottom: var(--dcn-space-4);
}

.drawer-section-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin: var(--dcn-space-3) 0 var(--dcn-space-2);
}

.disk-table {
  margin-bottom: var(--dcn-space-3);
}

.history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.history-chart {
  height: 260px;
}

.io-chart {
  height: 180px;
  margin-bottom: var(--dcn-space-4);
}

.chart {
  height: 100%;
  width: 100%;
}
</style>
