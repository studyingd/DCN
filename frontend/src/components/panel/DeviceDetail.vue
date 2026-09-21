<template>
  <div class="device-detail">
    <template v-if="deviceStore.currentDevice">
      <div class="detail-header">
        <el-icon :size="20" class="device-header-icon"><Monitor /></el-icon>
        <h3 class="detail-title">{{ deviceStore.currentDevice.name }}</h3>
        <el-tag :type="statusTagType" size="small" effect="dark" style="margin-left: auto">
          {{ statusLabel }}
        </el-tag>
      </div>

      <el-descriptions :column="1" border size="small" class="detail-descriptions">
        <el-descriptions-item label="设备类型">
          {{ deviceStore.currentDevice.type || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="IP 地址">
          <code>{{ deviceStore.currentDevice.ip_address || '-' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="操作系统">
          <span>{{ deviceStore.currentDevice.os_system || '-' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="位置 (U)">
          {{ deviceStore.currentDevice.position_u != null ? `${deviceStore.currentDevice.position_u} U` : '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="尺寸 (U)">
          {{ deviceStore.currentDevice.size_u != null ? `${deviceStore.currentDevice.size_u} U` : '-' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="showSshPort" label="SSH 端口">
          {{ deviceStore.currentDevice.ssh_port || '-' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="showRdpPort" label="RDP 端口">
          {{ deviceStore.currentDevice.rdp_port || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="所属机柜 ID">
          {{ deviceStore.currentDevice.rack_id }}
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">
          {{ formatDate(deviceStore.currentDevice.created_at) }}
        </el-descriptions-item>
      </el-descriptions>

      <!-- 纳管设备(服务器/云服务器/主机):实时性能指标(30s 自动刷新) -->
      <div v-if="isServerDevice" class="metrics-section">
        <div class="metrics-header">
          <span class="metrics-title">性能指标</span>
          <span v-if="metrics?.fetched_at" class="metrics-time" :class="{ 'is-stale': isStale(metrics.fetched_at) }">
            {{ fmtClock(metrics.fetched_at) }} 采集
            <el-tag v-if="isStale(metrics.fetched_at)" size="small" type="warning" effect="plain">过期</el-tag>
          </span>
          <el-button text size="small" type="primary" class="metrics-more" @click="goMetricsPanel">
            监控详情
          </el-button>
        </div>

        <template v-if="metrics && metrics.available">
          <div v-for="m in metricBars" :key="m.label" class="metric-row">
            <span class="metric-label">{{ m.label }}</span>
            <el-progress
              :percentage="m.value ?? 0"
              :color="metricColor(m.value, m.metric)"
              :stroke-width="10"
              class="metric-bar"
            />
          </div>
          <div class="metrics-meta">
            <div class="metrics-meta-row">负载 1/5/15:{{ fmtLoad(metrics) }}</div>
            <div class="metrics-meta-row">运行时间:{{ fmtUptime(metrics.uptime_sec) }}</div>
            <div class="metrics-meta-row">
              网络 ↓/↑:{{ fmtBps(metrics.net_rx_bps) }} / {{ fmtBps(metrics.net_tx_bps) }}
            </div>
            <div class="metrics-meta-row">
              磁盘 IO 读/写:{{ fmtBps(metrics.disk_read_bps) }} / {{ fmtBps(metrics.disk_write_bps) }}
            </div>
          </div>
        </template>
        <div v-else class="metrics-na">
          {{ metrics?.error || (metrics ? '采集不可用' : '加载中…') }}
        </div>
      </div>

      <div class="detail-actions">
        <el-button
          v-if="osIsLinux && authStore.hasPermission('device:remote')"
          type="primary"
          style="flex: 1"
          @click="$emit('connect-ssh')"
        >
          <el-icon><Link /></el-icon>
          SSH 连接
        </el-button>
        <el-button
          v-if="osIsWindows && authStore.hasPermission('device:remote')"
          type="success"
          style="flex: 1"
          @click="$emit('connect-rdp')"
        >
          <el-icon><Monitor /></el-icon>
          RDP 连接
        </el-button>
      </div>
    </template>

    <template v-else>
      <div class="detail-empty">
        <el-icon :size="48" style="color: var(--dcn-text-placeholder)"><Monitor /></el-icon>
        <p class="empty-text">请选择设备查看详情</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Monitor, Link } from '@element-plus/icons-vue'
import { useDeviceStore } from '@/stores/device'
import { useAuthStore } from '@/stores/auth'
import { metricsAPI } from '@/api'
import type { MetricsDeviceDetail } from '@/types/metrics'
import { useMetricColor } from '@/composables/useMetricColor'
import { fmtBps, fmtClock, fmtUptime, type ThresholdMetric } from '@/utils/metrics'
import { isSshOs, isWindowsOs } from '@/utils/osType'
import { isOpsDevice } from '@/utils/deviceLabels'

function isStale(ts: string | null | undefined, maxAgeSeconds = 120): boolean {
  return !!ts && Date.now() - new Date(ts).getTime() > maxAgeSeconds * 1000
}

const emit = defineEmits<{
  'connect-ssh': []
  'connect-rdp': []
  'open-metrics': [deviceId: number]
}>()

const deviceStore = useDeviceStore()
const authStore = useAuthStore()

const statusTagType = computed(() => {
  const status = deviceStore.currentDevice?.status?.toLowerCase()
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  return 'danger'
})

const device = computed(() => deviceStore.currentDevice)

// OS 判定走全项目统一口径（utils/osType）：含 "windows" → RDP，其余 → SSH。
// 不能要求字符串里必须出现 "linux"：云服务器探测回来的往往是
// "Ubuntu 3ubuntu0.17"、"Debian 7+deb13u4" 这类发行版名，会导致两个按钮都不显示。
const osIsWindows = computed(() => isWindowsOs(device.value?.os_system))
const osIsLinux = computed(() => isSshOs(device.value?.os_system))
// Port visibility: Windows → RDP only; Linux → SSH only; unknown → both
const showSshPort = computed(() => !osIsWindows.value)
const showRdpPort = computed(() => osIsWindows.value || !device.value?.os_system)

const statusLabel = computed(() => {
  const status = deviceStore.currentDevice?.status?.toLowerCase()
  if (status === 'online') return '在线'
  if (status === 'offline') return '离线'
  return '离线'
})

// ── 性能指标(纳管设备,30s 轮询) ──
// 类型判定走 deviceLabels 的共享集合,与后端 OPS_TARGET_TYPES 对齐——
// 云服务器同样有 SSH/WinRM 通道,过去写死 ['server','host'] 会让它整块指标区不渲染。
const isServerDevice = computed(() => isOpsDevice(device.value?.type))

const metrics = ref<MetricsDeviceDetail | null>(null)
let metricsTimer: number | undefined
let metricsInFlight = false
// 阈值配色:按指标查后端下发的阈值表,表还没到位时回退统一 70/90
const metricColor = useMetricColor()

async function loadMetrics() {
  const id = device.value?.id
  if (!id || !isServerDevice.value || metricsInFlight) return
  metricsInFlight = true
  try {
    const res = await metricsAPI.detail(id)
    metrics.value = res.data
  } catch {
    // 保留旧值,下轮再试
  } finally {
    metricsInFlight = false
  }
}

watch(
  () => device.value?.id,
  () => {
    metrics.value = null
    loadMetrics()
  },
)

onMounted(() => {
  loadMetrics()
  metricsTimer = window.setInterval(loadMetrics, 30000)
})
onUnmounted(() => window.clearInterval(metricsTimer))

const metricBars = computed(() => {
  if (!metrics.value) return []
  return [
    { label: 'CPU', value: metrics.value.cpu_pct, metric: 'cpu' as ThresholdMetric },
    { label: '内存', value: metrics.value.mem_pct, metric: 'memory' as ThresholdMetric },
    { label: '磁盘', value: metrics.value.disk_max_pct, metric: 'disk' as ThresholdMetric },
  ]
})

function fmtLoad(m: MetricsDeviceDetail): string {
  if (m.load1 == null) return ' -'
  const l5 = m.load5 != null ? m.load5.toFixed(2) : '-'
  const l15 = m.load15 != null ? m.load15.toFixed(2) : '-'
  return ` ${m.load1.toFixed(2)} / ${l5} / ${l15}`
}

function goMetricsPanel() {
  const id = device.value?.id
  if (id) emit('open-metrics', id)
}

function formatDate(dateStr: string): string {
  if (!dateStr) return '-'
  try {
    return new Date(dateStr).toLocaleString('zh-CN')
  } catch {
    return dateStr
  }
}
</script>

<style scoped>
.device-detail {
  padding: var(--dcn-space-4);
  height: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-4);
  padding-bottom: var(--dcn-space-3);
  border-bottom: 1px solid var(--dcn-border);
}

.device-header-icon {
  color: var(--dcn-warning);
}

.detail-title {
  margin: 0;
  font-size: var(--dcn-text-lg);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.detail-descriptions {
  flex: 1;
  overflow-y: auto;
}

.detail-descriptions :deep(.el-descriptions__label) {
  width: 100px;
  font-weight: 500;
  color: var(--dcn-text-regular);
}

.detail-descriptions code {
  background: var(--dcn-bg-section);
  padding: 2px 6px;
  border-radius: var(--dcn-radius-sm);
  font-size: var(--dcn-text-base);
  color: var(--dcn-primary);
}

.detail-actions {
  display: flex;
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-4);
  padding-top: var(--dcn-space-3);
  border-top: 1px solid var(--dcn-border);
}

.detail-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: var(--dcn-space-3);
}

/* ── 性能指标区 ── */
.metrics-section {
  margin-top: var(--dcn-space-4);
  padding-top: var(--dcn-space-3);
  border-top: 1px solid var(--dcn-border);
}

.metrics-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-2);
}

.metrics-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.metrics-time {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

.metrics-more {
  margin-left: auto;
}

.metric-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-1);
}

.metric-label {
  width: 36px;
  flex-shrink: 0;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
}

.metric-bar {
  flex: 1;
}

.metrics-meta {
  margin-top: var(--dcn-space-2);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
}

.metrics-meta-row {
  line-height: 1.9;
}

.metrics-na {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  padding: var(--dcn-space-2) 0;
}

.empty-text {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-md);
  margin: 0;
}
</style>
