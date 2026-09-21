<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  ArrowRight,
  Bell,
  Briefcase,
  CircleCheck,
  Monitor,
  Odometer,
  OfficeBuilding,
  Refresh,
  WarningFilled,
} from '@element-plus/icons-vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { metricsAPI } from '@/api'
import { useBigScreenPoller } from '@/composables/useBigScreenPoller'
import type { DeviceMetricTrend, MetricsRange } from '@/types/metrics'

use([CanvasRenderer, LineChart, GridComponent, LegendComponent, TooltipComponent])

const emit = defineEmits<{ navigate: [panel: string] }>()

const { overview, distribution, businesses, lastUpdated, loading, error, start, stop, refresh } = useBigScreenPoller()

const onlineRate = computed(() => {
  if (!overview.value.device_total) return 0
  return Math.round((overview.value.device_online / overview.value.device_total) * 100)
})

const exceptionCount = computed(() => overview.value.device_offline)

const deviceTypes = computed(() => {
  const order: Record<string, number> = {
    server: 0,
    cloud_server: 1,
    vm: 2,
    host: 3,
  }
  return [...distribution.value]
    .map((item) => (item.type === 'vm' ? { ...item, label: '虚拟机' } : item))
    .sort((a, b) => (order[a.type] ?? 99) - (order[b.type] ?? 99))
})

const businessStats = computed(() => {
  const stats = { total: businesses.value.length, healthy: 0, degraded: 0, down: 0, unknown: 0 }
  for (const item of businesses.value) {
    if (item.health === 'healthy') stats.healthy++
    else if (item.health === 'degraded') stats.degraded++
    else if (item.health === 'down') stats.down++
    else stats.unknown++
  }
  return stats
})

const businessComponentStats = computed(() =>
  businesses.value.reduce(
    (acc, item) => {
      acc.serverTotal += item.server_total
      acc.serverOnline += item.server_online
      acc.interfaceTotal += item.interface_total
      acc.interfaceUp += item.interface_up
      return acc
    },
    { serverTotal: 0, serverOnline: 0, interfaceTotal: 0, interfaceUp: 0 },
  ),
)

// 健康率环与左卡「在线率」同规格;环色跟随最差状态,让环本身携带信号而不只是装饰。
const businessHealthRate = computed(() => {
  const stats = businessStats.value
  if (!stats.total) return 0
  return Math.round((stats.healthy / stats.total) * 100)
})

const businessRingColor = computed(() => {
  const stats = businessStats.value
  if (!stats.total) return 'var(--dcn-text-placeholder)'
  if (stats.down) return 'var(--dcn-danger)'
  if (stats.degraded) return 'var(--dcn-warning)'
  if (stats.unknown) return 'var(--dcn-primary-light)'
  return 'var(--dcn-success)'
})

// 服务器在线率 / 接口可用率:原先挤在 10px 页脚里(比项目最小字号 token 还小),
// 现提升为带进度条的统计行,text 预拼好避免模板换行在数字里插入空白。
const businessRates = computed(() => {
  const stats = businessComponentStats.value
  const build = (up: number, total: number) => {
    const rate = total ? Math.round((up / total) * 100) : 0
    return { rate, text: `${up}/${total} · ${rate}%` }
  }
  return {
    server: build(stats.serverOnline, stats.serverTotal),
    interface: build(stats.interfaceUp, stats.interfaceTotal),
  }
})

const nonHealthyCount = computed(() => businessStats.value.total - businessStats.value.healthy)

// 「需关注业务」硬上限 2 行:业务再多也不会把这张卡片拉长,明细走「查看业务监控」。
// 取 2 而非 3 是为了和左卡等高(设备卡约 338px):无小标题时 2 行约 332px 基本齐平,
// 3 行约 366px 会反超设备卡、把空白挤到左边;残余几像素由 .business-card 的 flex 余量吸收。
const ATTENTION_LIMIT = 2
const HEALTH_RANK: Record<string, number> = { down: 0, degraded: 1, unknown: 2 }
const HEALTH_LABELS: Record<string, string> = {
  healthy: '健康',
  degraded: '降级',
  down: '中断',
  unknown: '未配置',
}
const HEALTH_DOT_CLASS: Record<string, string> = {
  healthy: 'status-dot--online',
  degraded: 'status-dot--degraded',
  down: 'status-dot--offline',
  unknown: 'status-dot--unknown',
}

const attentionBusinesses = computed(() =>
  businesses.value
    .filter((item) => item.health !== 'healthy')
    .sort((a, b) => (HEALTH_RANK[a.health] ?? 99) - (HEALTH_RANK[b.health] ?? 99))
    .slice(0, ATTENTION_LIMIT),
)

// ── 服务器 CPU / 内存曲线(机房管理纳管的全部 server/host) ──
const TREND_RANGES: Array<{ value: MetricsRange; label: string }> = [
  { value: '1h', label: '1小时' },
  { value: '6h', label: '6小时' },
  { value: '24h', label: '24小时' },
  { value: '7d', label: '7天' },
]
const TREND_COLORS = [
  '#60a5fa',
  '#a78bfa',
  '#34d399',
  '#fbbf24',
  '#f87171',
  '#38bdf8',
  '#f472b6',
  '#4ade80',
  '#fb923c',
  '#818cf8',
]
const TREND_POLL_MS = 60_000

const trendRange = ref<MetricsRange>('1h')
const trendSeries = ref<DeviceMetricTrend[]>([])
const trendLoading = ref(false)
const trendError = ref('')
let trendTimer: number | undefined
let trendRequest: Promise<void> | null = null

const trendDevices = computed(() => trendSeries.value.filter((item) => item.points.length > 0))
const hasTrendData = computed(() => trendDevices.value.length > 0)
const trendEmptyText = computed(() => trendError.value || '暂无曲线数据，服务器指标采集满一个周期后开始展示')

function trendStat(metric: 'cpu_pct' | 'mem_pct') {
  const values: number[] = []
  for (const device of trendDevices.value) {
    for (const point of device.points) {
      const value = point[metric]
      if (value != null) values.push(value)
    }
  }
  if (values.length === 0) return null
  const total = values.reduce((sum, value) => sum + value, 0)
  const peak = values.reduce((max, value) => Math.max(max, value), 0)
  return { avg: (total / values.length).toFixed(1), peak: peak.toFixed(1) }
}

const cpuStat = computed(() => trendStat('cpu_pct'))
const memStat = computed(() => trendStat('mem_pct'))

function buildTrendOption(metric: 'cpu_pct' | 'mem_pct') {
  return {
    animation: false,
    color: TREND_COLORS,
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111827',
      borderColor: '#223047',
      textStyle: { color: '#f1f5f9', fontSize: 12 },
      valueFormatter: (value: number | null) => (value == null ? '-' : `${Number(value).toFixed(1)}%`),
    },
    legend: { type: 'scroll', top: 0, right: 4, textStyle: { color: '#94a3b8', fontSize: 11 } },
    grid: { left: 48, right: 18, top: 34, bottom: 28 },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#223047' } },
      axisLabel: { color: '#64748b', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { color: '#64748b', fontSize: 10, formatter: '{value}%' },
      splitLine: { lineStyle: { color: '#18243a' } },
    },
    series: trendDevices.value.map((device, index) => ({
      id: `trend-${device.device_id}`,
      name: device.device_name,
      type: 'line' as const,
      showSymbol: false,
      connectNulls: false,
      smooth: 0.2,
      lineStyle: { width: 1.8 },
      areaStyle: { opacity: 0.05 },
      // itemStyle 会被 setOption 增量合并进旧 series 导致首条颜色固化——每台设备固定
      // 槽位色必须写进 series 自身颜色字段，且选项经 notMerge 整体重建
      color: TREND_COLORS[index % TREND_COLORS.length],
      emphasis: { focus: 'series' as const },
      data: device.points.map((point) => [new Date(point.ts).getTime(), point[metric]]),
    })),
  }
}

const cpuChartOption = computed(() => buildTrendOption('cpu_pct'))
const memChartOption = computed(() => buildTrendOption('mem_pct'))
// 轮询/切范围时 series 数量与顺序都会变；v-chart 按 id 做增量合并，会把上一轮的
// itemStyle 合并进新 series，造成“所有折线同色”。加递增 key 触发组件整体重建，
// 每次都用全新 option 渲染
const trendOptionEpoch = ref(0)
watch([cpuChartOption, memChartOption], () => {
  trendOptionEpoch.value++
})

async function loadTrend() {
  if (trendRequest) return trendRequest
  const request = (async () => {
    trendLoading.value = true
    try {
      const res = await metricsAPI.trend(trendRange.value)
      trendSeries.value = res.data.series
      trendError.value = ''
    } catch {
      trendError.value = '服务器指标曲线加载失败，请稍后重试'
    } finally {
      trendLoading.value = false
    }
  })()
  trendRequest = request
  try {
    await request
  } finally {
    if (trendRequest === request) trendRequest = null
  }
}

function refreshHome() {
  void refresh()
  void loadTrend()
}

watch(trendRange, () => {
  void loadTrend()
})

function typeColor(type: string): string {
  const colors: Record<string, string> = {
    server: 'var(--dcn-device-server)',
    cloud_server: 'var(--dcn-device-cloud-server)',
    host: 'var(--dcn-device-host)',
    vm: 'var(--dcn-primary-light)',
  }
  return colors[type] || 'var(--dcn-primary)'
}

onMounted(() => {
  start()
  void loadTrend()
  trendTimer = window.setInterval(() => {
    if (!document.hidden) void loadTrend()
  }, TREND_POLL_MS)
})

onUnmounted(() => {
  stop()
  window.clearInterval(trendTimer)
})
</script>

<template>
  <div class="home-panel">
    <div v-loading="loading && !lastUpdated" class="home-scroll">
      <header class="home-header">
        <div>
          <h1>数据中心运行概览</h1>
        </div>
        <div class="header-actions">
          <div class="update-state" :class="{ 'is-loading': loading, 'has-error': error }">
            <span class="live-dot" />
            <span>{{
              error
                ? '部分数据加载异常'
                : loading
                  ? '正在更新数据'
                  : lastUpdated
                    ? `更新于 ${lastUpdated}`
                    : '正在获取最新数据'
            }}</span>
          </div>
          <el-button
            :icon="Refresh"
            :loading="loading || trendLoading"
            circle
            aria-label="刷新首页数据"
            @click="refreshHome"
          />
        </div>
      </header>

      <div v-if="error" class="home-alert" role="status">
        <el-icon><WarningFilled /></el-icon>
        <span>{{ error }}</span>
        <el-button link type="primary" size="small" @click="refresh">重试</el-button>
      </div>

      <section class="kpi-grid">
        <article class="kpi-card">
          <div class="kpi-icon kpi-icon--purple"><OfficeBuilding /></div>
          <div class="kpi-content infrastructure-kpi-content">
            <span class="kpi-label">基础设施</span>
            <strong>{{ overview.rack_count + overview.pve_platform_count }} <small>项</small></strong>
            <span class="kpi-meta"
              >{{ overview.rack_count }} 个机柜 · {{ overview.pve_platform_count }} 个 PVE 平台</span
            >
          </div>
          <div class="kpi-content">
            <span class="kpi-label">基础设施</span>
            <strong>{{ overview.room_count }} <small>机房</small></strong>
            <span class="kpi-meta">共 {{ overview.rack_count }} 个机柜</span>
          </div>
        </article>
        <article class="kpi-card">
          <div class="kpi-icon kpi-icon--blue"><Monitor /></div>
          <div class="kpi-content">
            <span class="kpi-label">纳管设备</span>
            <strong>{{ overview.device_total }} <small>台</small></strong>
            <span class="kpi-meta">{{ overview.device_online }} 台当前在线</span>
          </div>
        </article>
        <article class="kpi-card">
          <div class="kpi-icon" :class="exceptionCount ? 'kpi-icon--warning' : 'kpi-icon--success'">
            <WarningFilled v-if="exceptionCount" />
            <Odometer v-else />
          </div>
          <div class="kpi-content">
            <span class="kpi-label">运行健康度</span>
            <strong>{{ onlineRate }}<small>%</small></strong>
            <span class="kpi-meta" :class="{ 'has-warning': exceptionCount }">
              {{ exceptionCount ? `${exceptionCount} 台设备需关注` : '所有设备状态正常' }}
            </span>
          </div>
        </article>
        <article class="kpi-card">
          <div class="kpi-icon" :class="overview.alert_events_7d ? 'kpi-icon--warning' : 'kpi-icon--success'">
            <Bell />
          </div>
          <div class="kpi-content">
            <span class="kpi-label">告警数量</span>
            <strong>{{ overview.alert_events_7d }} <small>条</small></strong>
            <span class="kpi-meta" :class="{ 'has-warning': overview.alert_events_7d }">近 7 天累计触发</span>
          </div>
        </article>
      </section>

      <div class="content-grid">
        <section class="home-card health-card">
          <div class="card-heading">
            <div>
              <span class="section-kicker">DEVICE HEALTH</span>
              <h2>设备运行状态</h2>
            </div>
            <button class="text-action" type="button" @click="emit('navigate', 'metrics')">
              查看监控 <el-icon><ArrowRight /></el-icon>
            </button>
          </div>

          <div class="health-overview">
            <div class="health-score">
              <div class="score-ring" :style="{ '--rate': `${onlineRate * 3.6}deg` }">
                <div class="score-ring__inner">
                  <strong>{{ onlineRate }}%</strong>
                  <span>在线率</span>
                </div>
              </div>
            </div>
            <div class="status-list">
              <div class="status-row">
                <span><i class="status-dot status-dot--online" />在线</span
                ><strong>{{ overview.device_online }}</strong>
              </div>
              <div class="status-row">
                <span><i class="status-dot status-dot--offline" />离线</span
                ><strong>{{ overview.device_offline }}</strong>
              </div>
            </div>
          </div>

          <div class="type-list">
            <div v-for="item in deviceTypes" :key="item.type" class="type-row">
              <div class="type-meta">
                <span>{{ item.label || item.type }}</span
                ><strong>{{ item.count }}</strong>
              </div>
              <div class="type-track">
                <span
                  :style="{
                    width: `${overview.device_total ? (item.count / overview.device_total) * 100 : 0}%`,
                    background: typeColor(item.type),
                  }"
                />
              </div>
            </div>
            <el-empty v-if="deviceTypes.length === 0" description="暂无设备数据" :image-size="48" />
          </div>
        </section>

        <section class="home-card business-card">
          <div class="card-heading">
            <div>
              <span class="section-kicker">BUSINESS HEALTH</span>
              <h2>业务运行状态</h2>
            </div>
            <button class="text-action" type="button" @click="emit('navigate', 'business')">
              查看业务监控 <el-icon><ArrowRight /></el-icon>
            </button>
          </div>

          <!-- 概览行与左卡「在线率环 + 在线/离线」严格同构:健康率环 + 四态计数。
               右列 4 行压紧后约 118px,正好吃满 126px 环高的空档,不额外撑高卡片。 -->
          <div class="business-overview">
            <div class="business-score">
              <div
                class="score-ring"
                :style="{ '--rate': `${businessHealthRate * 3.6}deg`, '--ring-color': businessRingColor }"
              >
                <div class="score-ring__inner">
                  <strong>{{ businessHealthRate }}%</strong>
                  <span>健康率</span>
                </div>
              </div>
            </div>
            <div class="status-list" aria-label="业务健康统计">
              <div class="status-row">
                <span><i class="status-dot status-dot--online" />健康</span
                ><strong class="count--healthy">{{ businessStats.healthy }}</strong>
              </div>
              <div class="status-row">
                <span><i class="status-dot status-dot--degraded" />降级</span
                ><strong class="count--degraded">{{ businessStats.degraded }}</strong>
              </div>
              <div class="status-row">
                <span><i class="status-dot status-dot--offline" />中断</span
                ><strong class="count--down">{{ businessStats.down }}</strong>
              </div>
              <div class="status-row">
                <span><i class="status-dot status-dot--unknown" />未配置</span
                ><strong class="count--unknown">{{ businessStats.unknown }}</strong>
              </div>
            </div>
          </div>

          <!-- 刻意不渲染完整业务列表：业务一多这张卡片会被无限拉长，把下面的趋势图挤到首屏之外。
               改为「有界」填充——概览行高度固定，需关注业务硬上限 ATTENTION_LIMIT 行，
               明细走右上角「查看业务监控」。 -->
          <div v-if="!businessStats.total" class="business-empty">
            <el-icon><Briefcase /></el-icon>
            <span>尚未配置业务监控</span>
            <el-button link type="primary" size="small" @click="emit('navigate', 'business')">前往配置</el-button>
          </div>

          <div v-else-if="!nonHealthyCount" class="business-clear">
            <el-icon><CircleCheck /></el-icon>
            <span>全部 {{ businessStats.total }} 个业务运行正常</span>
            <span class="business-clear__meta"
              >服务器 {{ businessRates.server.text }} · 接口 {{ businessRates.interface.text }}</span
            >
          </div>

          <!-- 不显示小标题:非健康业务的数量已由概览行的「降级/中断/未配置」计数表达。
               可见标题去掉后无障碍名称也一起没了,用 aria-label 补回给读屏软件。 -->
          <div v-else class="business-attention" role="list" aria-label="需关注业务">
            <div v-for="item in attentionBusinesses" :key="item.id" class="attention-row" role="listitem">
              <i class="status-dot" :class="HEALTH_DOT_CLASS[item.health] ?? 'status-dot--unknown'" />
              <span class="attention-name" :title="item.name">{{ item.name }}</span>
              <span class="attention-state">{{ HEALTH_LABELS[item.health] ?? item.health }}</span>
              <span class="attention-metric">服务器 {{ item.server_online }}/{{ item.server_total }}</span>
              <span class="attention-metric">接口 {{ item.interface_up }}/{{ item.interface_total }}</span>
            </div>
          </div>
        </section>

        <section class="home-card trend-card">
          <div class="card-heading">
            <div>
              <span class="section-kicker">CPU TREND</span>
              <h2>服务器 CPU 概览</h2>
            </div>
            <div class="trend-actions">
              <span class="trend-meta">
                {{ cpuStat ? `${trendDevices.length} 台 · 均值 ${cpuStat.avg}% · 峰值 ${cpuStat.peak}%` : '暂无数据' }}
              </span>
              <el-radio-group v-model="trendRange" size="small" aria-label="CPU 曲线时间范围">
                <el-radio-button v-for="item in TREND_RANGES" :key="item.value" :value="item.value">{{
                  item.label
                }}</el-radio-button>
              </el-radio-group>
              <button class="text-action" type="button" @click="emit('navigate', 'metrics')">
                服务器监控 <el-icon><ArrowRight /></el-icon>
              </button>
            </div>
          </div>
          <div v-loading="trendLoading" class="trend-chart">
            <v-chart
              v-if="hasTrendData"
              :key="`cpu-${trendOptionEpoch}`"
              :option="cpuChartOption"
              autoresize
              class="trend-canvas"
            />
            <el-empty v-else-if="!trendLoading" :description="trendEmptyText" :image-size="64" />
          </div>
        </section>

        <section class="home-card trend-card">
          <div class="card-heading">
            <div>
              <span class="section-kicker">MEMORY TREND</span>
              <h2>服务器内存概览</h2>
            </div>
            <div class="trend-actions">
              <span class="trend-meta">
                {{ memStat ? `${trendDevices.length} 台 · 均值 ${memStat.avg}% · 峰值 ${memStat.peak}%` : '暂无数据' }}
              </span>
              <el-radio-group v-model="trendRange" size="small" aria-label="内存曲线时间范围">
                <el-radio-button v-for="item in TREND_RANGES" :key="item.value" :value="item.value">{{
                  item.label
                }}</el-radio-button>
              </el-radio-group>
            </div>
          </div>
          <div v-loading="trendLoading" class="trend-chart">
            <v-chart
              v-if="hasTrendData"
              :key="`mem-${trendOptionEpoch}`"
              :option="memChartOption"
              autoresize
              class="trend-canvas"
            />
            <el-empty v-else-if="!trendLoading" :description="trendEmptyText" :image-size="64" />
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.home-panel {
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
}
.home-scroll {
  height: 100%;
  overflow-y: auto;
  padding: 28px 30px 36px;
}
.home-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--dcn-space-6);
  margin-bottom: var(--dcn-space-6);
}
/* 页头 eyebrow 与副标题已移除；section-kicker 仍用于各分区小标题 */
.section-kicker {
  color: var(--dcn-primary-light);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1.7px;
}
.home-header h1 {
  /* 上方不再有 eyebrow、下方不再有副标题，留白交给 .home-header 自己控制 */
  margin: 0;
  font-size: 25px;
  line-height: 1.2;
  font-weight: 650;
}
.home-alert {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin: -8px 0 var(--dcn-space-4);
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--dcn-warning) 35%, var(--dcn-border));
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-warning-light);
  color: var(--dcn-warning-text);
  font-size: var(--dcn-text-sm);
}
.home-alert span {
  flex: 1;
}
.header-actions,
.update-state {
  display: flex;
  align-items: center;
}
.header-actions {
  gap: var(--dcn-space-3);
}
.update-state {
  gap: 7px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--dcn-success);
  box-shadow: 0 0 8px color-mix(in srgb, var(--dcn-success) 65%, transparent);
}
.update-state.is-loading .live-dot {
  background: var(--dcn-info);
  box-shadow: none;
  animation: home-pulse 1.2s ease-in-out infinite;
}
.update-state.has-error {
  color: var(--dcn-warning-text);
}
.update-state.has-error .live-dot {
  background: var(--dcn-warning);
  box-shadow: none;
  animation: none;
}
@keyframes home-pulse {
  50% {
    opacity: 0.35;
  }
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.kpi-card,
.home-card {
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-xl);
  box-shadow: var(--dcn-shadow-sm);
}
.kpi-card {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  min-height: 108px;
  padding: var(--dcn-space-4);
}
.kpi-icon {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  flex-shrink: 0;
  border-radius: var(--dcn-radius-lg);
  font-size: 20px;
}
.kpi-icon--purple {
  color: var(--dcn-primary-light);
  background: var(--dcn-primary-bg-deep);
}
.kpi-icon--blue {
  color: var(--dcn-device-server);
  background: var(--dcn-device-server-bg);
}
.kpi-icon--success {
  color: var(--dcn-success);
  background: var(--dcn-success-light);
}
.kpi-icon--warning {
  color: var(--dcn-warning);
  background: var(--dcn-warning-light);
}
.kpi-content {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.kpi-card:first-child > .kpi-content:not(.infrastructure-kpi-content) {
  display: none;
}
.kpi-label {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.kpi-content strong {
  margin: 4px 0;
  color: var(--dcn-text-primary);
  font: 700 25px/1 var(--dcn-font-mono);
}
.kpi-content strong small {
  font: 500 var(--dcn-text-xs) var(--dcn-font-sans);
  color: var(--dcn-text-secondary);
}
.kpi-meta {
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}
.kpi-meta.has-warning {
  color: var(--dcn-warning);
}

.content-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--dcn-space-4);
}
.home-card {
  padding: var(--dcn-space-5);
}
/* 两张卡在 .content-grid 里会被拉伸到同高。让各自的末段吃掉余量,
   多出来的高度就落在列表区内部,而不是卡片底部一整块裸空白。 */
.health-card,
.business-card {
  display: flex;
  flex-direction: column;
}
.health-card .type-list {
  flex: 1 1 auto;
  align-content: start;
}
.trend-card {
  grid-column: 1 / -1;
}
.card-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-4);
  margin-bottom: var(--dcn-space-4);
}
.card-heading h2 {
  margin-top: 3px;
  font-size: var(--dcn-text-lg);
  font-weight: 600;
}
.text-action {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 0;
  background: transparent;
  color: var(--dcn-text-secondary);
  font: inherit;
  font-size: var(--dcn-text-xs);
  cursor: pointer;
}
.text-action:hover {
  color: var(--dcn-primary-light);
}

/* 概览行(环形 + 右侧统计)是两张卡共用的骨架,业务卡直接复用同一套声明 */
.health-overview,
.business-overview {
  display: grid;
  grid-template-columns: 160px 1fr;
  align-items: center;
  gap: var(--dcn-space-5);
  padding: var(--dcn-space-2) 0 var(--dcn-space-5);
  border-bottom: 1px solid var(--dcn-border-light);
}
.health-score,
.business-score {
  display: grid;
  place-items: center;
}
.score-ring {
  width: 126px;
  height: 126px;
  padding: 8px;
  border-radius: 50%;
  /* --ring-color 可选:业务卡按最差健康状态改色,设备卡沿用默认的成功绿 */
  background: conic-gradient(var(--ring-color, var(--dcn-success)) var(--rate), var(--dcn-bg-muted) 0);
}
.score-ring__inner {
  display: flex;
  width: 100%;
  height: 100%;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--dcn-bg-card);
}
.score-ring__inner strong {
  font: 700 25px var(--dcn-font-mono);
}
.score-ring__inner span {
  margin-top: 3px;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}
.status-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.status-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 10px;
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
}
.status-row:hover {
  background: var(--dcn-bg-muted);
}
.status-row span {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-row strong {
  color: var(--dcn-text-primary);
  font-family: var(--dcn-font-mono);
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.status-dot--online {
  background: var(--dcn-success);
}
.status-dot--offline {
  background: var(--dcn-danger);
}
.status-dot--degraded {
  background: var(--dcn-warning);
}
.status-dot--unknown {
  background: var(--dcn-text-placeholder);
}
.type-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px 22px;
  padding-top: var(--dcn-space-4);
}
.type-meta {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.type-meta strong {
  color: var(--dcn-text-regular);
  font-family: var(--dcn-font-mono);
}
.type-track {
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--dcn-bg-muted);
}
.type-track span {
  display: block;
  height: 100%;
  min-width: 3px;
  border-radius: inherit;
}

/* 概览行右列要塞下 4 行(左卡只有 2 行),收窄上下内边距后约 118px,
   仍小于 126px 的环高,因此不会把这一行撑高。 */
.business-overview .status-row {
  padding: 5px 10px;
}
/* 四态计数色。选择器需压过 .status-row strong 的 text-primary(0,1,1),
   两个 class 的特异性是 (0,2,0)。 */
.status-row .count--healthy {
  color: var(--dcn-success);
}
.status-row .count--degraded {
  color: var(--dcn-warning);
}
.status-row .count--down {
  color: var(--dcn-danger);
}
.status-row .count--unknown {
  color: var(--dcn-text-placeholder);
}
.business-empty {
  display: flex;
  flex: 1 1 auto;
  min-height: 138px;
  align-items: center;
  justify-content: center;
  gap: var(--dcn-space-2);
  color: var(--dcn-text-secondary);
}
/* 全部正常时的正向态,顺带承载原先挤在 10px 页脚里的两组聚合率 */
.business-clear {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-items: center;
  align-content: center;
  justify-content: center;
  gap: 2px var(--dcn-space-2);
  padding: var(--dcn-space-4) 10px var(--dcn-space-2);
  border-top: 1px solid var(--dcn-border-light);
  color: var(--dcn-success);
  font-size: var(--dcn-text-sm);
}
.business-clear__meta {
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
  font-size: var(--dcn-text-xs);
}

/* 需关注业务:行数由 ATTENTION_LIMIT 钉死,卡片高度不随业务数量增长 */
.business-attention {
  display: grid;
  flex: 1 1 auto;
  align-content: start;
  gap: 2px;
  padding-top: var(--dcn-space-3);
  border-top: 1px solid var(--dcn-border-light);
}
.attention-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px var(--dcn-space-3);
  padding: 7px 10px;
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
}
.attention-row:hover {
  background: var(--dcn-bg-muted);
}
.attention-name {
  flex: 1 1 120px;
  min-width: 0;
  overflow: hidden;
  color: var(--dcn-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attention-state,
.attention-metric {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.attention-metric {
  font-family: var(--dcn-font-mono);
}

.trend-actions {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}
.trend-meta {
  color: var(--dcn-text-placeholder);
  font: 500 var(--dcn-text-xs) var(--dcn-font-mono);
}
.trend-chart {
  min-height: 272px;
}
.trend-canvas {
  width: 100%;
  height: 272px;
}

@media (max-width: 1100px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .content-grid {
    grid-template-columns: 1fr;
  }
  .trend-card {
    grid-column: auto;
  }
}
@media (max-width: 720px) {
  .home-scroll {
    padding: var(--dcn-space-4);
  }
  .home-header {
    align-items: flex-start;
    flex-direction: column;
  }
  .kpi-grid,
  .type-list {
    grid-template-columns: 1fr;
  }
  .health-overview,
  .business-overview {
    grid-template-columns: 1fr;
  }
  .trend-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .trend-chart,
  .trend-canvas {
    min-height: 228px;
    height: 228px;
  }
}
</style>
