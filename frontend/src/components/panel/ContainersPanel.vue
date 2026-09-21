<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">容器管理</h2>
      <span class="ctr-desc">探测服务器及 PVE 虚拟机内的 Docker 容器 · 每 60s 自动采集</span>
    </div>
    <el-alert v-if="pollError" :title="pollError" type="warning" show-icon :closable="false" class="poll-alert" />

    <div class="ctr-body">
      <!-- 汇总条 -->
      <div class="ctr-summary">
        <div class="sum-item">
          <span class="sum-value">{{ summary.total }}</span>
          <span class="sum-label">容器总数</span>
        </div>
        <div class="sum-item">
          <span class="sum-value sum-running">{{ summary.running }}</span>
          <span class="sum-label">运行中</span>
        </div>
        <div class="sum-item">
          <span class="sum-value sum-stopped">{{ summary.stopped }}</span>
          <span class="sum-label">已停止</span>
        </div>
        <div class="sum-item">
          <span class="sum-value">{{ summary.dockerHosts }}</span>
          <span class="sum-label">Docker 目标</span>
        </div>
      </div>

      <!-- 工具栏 -->
      <div class="ctr-toolbar">
        <el-input
          v-model="keyword"
          placeholder="搜索设备 / 容器 / 镜像"
          prefix-icon="Search"
          clearable
          class="ctr-search"
        />
        <el-select v-model="stateFilter" class="ctr-filter" clearable placeholder="容器状态">
          <el-option label="全部状态" value="" />
          <el-option label="运行中" value="running" />
          <el-option label="已停止" value="exited" />
        </el-select>
        <div class="ctr-toolbar-right">
          <span class="ctr-updated" :class="{ 'is-stale': isStale(lastLoaded) }">
            {{ lastLoaded ? `数据时间 ${fmtTime(lastLoaded)}` : '暂无数据' }}
            <el-tag v-if="isStale(lastLoaded)" size="small" type="warning" effect="plain">数据可能已过期</el-tag>
          </span>
          <el-button :loading="loading" @click="load(false)">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
        </div>
      </div>

      <!-- 设备 + 容器卡片 -->
      <div v-loading="loading && entries.length === 0" class="ctr-list">
        <el-empty
          v-if="!loading && filteredEntries.length === 0"
          description="没有符合条件的服务器、虚拟机或容器"
          :image-size="70"
        />

        <section v-for="e in filteredEntries" :key="e.device_id" class="ctr-device">
          <header class="ctr-device-head">
            <div class="ctr-device-title">
              <span class="ctr-device-name">{{ e.device_name }}</span>
              <span class="ctr-device-ip">{{ e.ip_address || '-' }}</span>
              <el-tag v-if="e.target_type === 'pve_guest'" size="small" type="primary" effect="plain">
                PVE QEMU · VMID {{ e.pve_vmid }}
              </el-tag>
              <el-tag v-if="e.available" size="small" type="success" effect="plain">
                Docker {{ e.version || '' }}
              </el-tag>
              <el-tag v-else-if="e.target_type === 'pve_guest'" size="small" type="warning" effect="plain">
                {{ e.has_credential ? '等待采集' : '未配置凭据' }}
              </el-tag>
              <el-tag size="small" effect="plain">{{ e.container_count }} 容器</el-tag>
              <el-tag v-if="e.commands?.stats && !e.commands.stats.success" size="small" type="warning" effect="plain">
                资源统计不可用
              </el-tag>
              <span class="ctr-device-updated" :class="{ 'is-stale': isStale(e.updated_at) }">
                采集 {{ fmtTime(e.updated_at) }}<span v-if="isStale(e.updated_at)"> · 过期</span>
              </span>
            </div>
            <el-tooltip v-if="e.last_error" :content="e.last_error">
              <el-icon class="ctr-err-icon"><WarningFilled /></el-icon>
            </el-tooltip>
          </header>

          <div v-if="filterContainers(e.containers).length === 0" class="ctr-nodata-card">
            <template v-if="!e.available">{{ e.last_error || '尚未发现可用的 Docker 服务。' }}</template>
            <template v-else>Docker 运行中,但没有符合筛选条件的容器。</template>
          </div>

          <!-- 容器卡片网格:点击卡片查看详情 -->
          <div v-else class="ctr-grid">
            <div
              v-for="c in filterContainers(e.containers)"
              :key="c.container_id"
              class="c-card"
              :class="stateAccent(c.state)"
              role="button"
              tabindex="0"
              :aria-label="`打开容器 ${c.name} 详情`"
              @click="openDetail(e, c)"
              @keydown.enter="openDetail(e, c)"
              @keydown.space.prevent="openDetail(e, c)"
            >
              <div class="c-top">
                <span class="c-dot" :class="stateAccent(c.state)"></span>
                <span class="c-name" :title="c.name">{{ c.name }}</span>
                <el-tag class="c-state" size="small" :type="stateTagType(c.state)" effect="plain">
                  {{ stateLabel(c.state) }}
                </el-tag>
              </div>

              <div class="c-image" :title="c.image || undefined">{{ c.image || '-' }}</div>

              <div class="c-metrics">
                <div class="c-metric">
                  <span class="c-metric-label">CPU</span>
                  <el-progress
                    v-if="c.state === 'running' && c.cpu_pct != null"
                    :percentage="progressPct(c.cpu_pct)"
                    :color="metricColor(c.cpu_pct, 'cpu')"
                    :stroke-width="5"
                    :show-text="false"
                    class="c-bar"
                  />
                  <span v-else class="c-metric-empty">—</span>
                  <span class="c-metric-val">{{
                    c.state === 'running' && c.cpu_pct != null ? c.cpu_pct + '%' : ''
                  }}</span>
                </div>
                <div class="c-metric">
                  <span class="c-metric-label">内存</span>
                  <el-progress
                    v-if="c.state === 'running' && c.mem_pct != null"
                    :percentage="c.mem_pct"
                    :color="metricColor(c.mem_pct, 'memory')"
                    :stroke-width="5"
                    :show-text="false"
                    class="c-bar"
                  />
                  <span v-else class="c-metric-empty">—</span>
                  <span class="c-metric-val">{{
                    c.state === 'running' && c.mem_pct != null ? fmtMb(c.mem_used_mb) : ''
                  }}</span>
                </div>
              </div>

              <div class="c-foot">
                <span v-if="compactPorts(c.ports)" class="c-ports" :title="c.ports || undefined">
                  <el-icon><Connection /></el-icon>{{ compactPorts(c.ports) }}
                </span>
                <span class="c-more"
                  >详情<el-icon><ArrowRight /></el-icon
                ></span>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- 容器详情抽屉 -->
    <el-drawer
      v-model="detailVisible"
      :title="detailContainer ? detailContainer.name : '容器详情'"
      size="860px"
      direction="rtl"
      @close="closeTerminal"
    >
      <div v-if="detailEntry && detailContainer" class="d-body">
        <div class="d-status-row">
          <el-tag :type="stateTagType(detailContainer.state)" effect="plain" size="large">
            {{ stateLabel(detailContainer.state) }}
          </el-tag>
          <span class="d-image" :title="detailContainer.image || undefined">{{ detailContainer.image || '-' }}</span>
        </div>

        <el-descriptions :column="1" border size="small" class="d-desc">
          <el-descriptions-item label="容器 ID">
            <span class="d-mono">{{ detailContainer.container_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="运行信息">{{ detailContainer.status || '-' }}</el-descriptions-item>
          <el-descriptions-item label="端口映射">
            <div v-if="detailPorts.length === 0" class="d-ports-empty">无</div>
            <div v-else class="d-ports">
              <div v-for="(p, i) in detailPorts" :key="i" class="d-port-row">
                <el-icon class="d-port-icon"><Connection /></el-icon>
                <span class="d-port-text">{{ fmtPortRow(p) }}</span>
                <el-tag v-if="!p.hostPort" size="small" type="info" effect="plain">仅暴露</el-tag>
              </div>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="所属设备">
            {{ detailEntry.device_name }}<span v-if="detailEntry.ip_address"> ({{ detailEntry.ip_address }})</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="detailEntry.target_type === 'pve_guest'" label="PVE Guest">
            QEMU 虚拟机 · VMID {{ detailEntry.pve_vmid }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="d-section-title">资源占用</div>
        <div class="d-metrics">
          <div class="d-metric-row">
            <span class="d-metric-label">CPU</span>
            <el-progress
              v-if="detailContainer.state === 'running' && detailContainer.cpu_pct != null"
              :percentage="progressPct(detailContainer.cpu_pct)"
              :color="metricColor(detailContainer.cpu_pct, 'cpu')"
              :stroke-width="8"
              class="d-bar"
            />
            <span v-else class="d-metric-none">容器未运行</span>
          </div>
          <div class="d-metric-row">
            <span class="d-metric-label">内存</span>
            <el-progress
              v-if="detailContainer.state === 'running' && detailContainer.mem_pct != null"
              :percentage="detailContainer.mem_pct"
              :color="metricColor(detailContainer.mem_pct, 'memory')"
              :stroke-width="8"
              class="d-bar"
            />
            <span v-else class="d-metric-none">容器未运行</span>
          </div>
          <div v-if="detailContainer.mem_used_mb != null && detailContainer.mem_limit_mb != null" class="d-mem-detail">
            内存使用 {{ fmtMb(detailContainer.mem_used_mb) }} / {{ fmtMb(detailContainer.mem_limit_mb) }}
          </div>
        </div>

        <div class="d-section-title">操作</div>
        <div class="d-actions">
          <el-button
            v-if="canControl && detailContainer.state !== 'running'"
            type="success"
            :loading="actingKey === key(detailEntry.device_id, detailContainer.name, 'start')"
            @click="control(detailEntry, detailContainer, 'start')"
            >启动</el-button
          >
          <el-button
            v-if="canControl && detailContainer.state === 'running'"
            type="danger"
            :loading="actingKey === key(detailEntry.device_id, detailContainer.name, 'stop')"
            @click="control(detailEntry, detailContainer, 'stop')"
            >停止</el-button
          >
          <el-button
            v-if="canControl && detailContainer.state === 'running'"
            type="warning"
            :loading="actingKey === key(detailEntry.device_id, detailContainer.name, 'restart')"
            @click="control(detailEntry, detailContainer, 'restart')"
            >重启</el-button
          >
          <el-button
            v-if="canControl"
            type="danger"
            plain
            :loading="actingKey === key(detailEntry.device_id, detailContainer.name, 'remove')"
            @click="confirmRemove(detailEntry, detailContainer)"
            >删除</el-button
          >
          <el-button plain @click="openLogs(detailEntry, detailContainer)">
            <el-icon><Document /></el-icon>查看日志
          </el-button>
          <el-button
            v-if="canContainerTerminal && detailContainer.state === 'running'"
            type="primary"
            plain
            :loading="terminalLoading"
            @click="toggleTerminal"
          >
            <el-icon><Monitor /></el-icon>{{ showTerminal ? '收起终端' : '进入终端' }}
          </el-button>
        </div>

        <!-- 内嵌容器终端(docker exec)。设备目标走设备终端链路;PVE 虚机走
             pve-terminal 链路(后端从 binding 解析凭据,票据带 container)。
             不开 enable-files:宿主/虚机文件传输去机房管理/虚拟化管理的 SSH
             入口做,容器终端保持纯 exec(用户定调,勿加回)。 -->
        <div v-if="showTerminal" class="d-terminal">
          <Terminal
            v-if="detailEntry.target_type === 'pve_guest'"
            :key="`pve-${detailEntry.device_id}-${detailContainer.name}`"
            minimal
            :device-id="detailEntry.device_id"
            conn-type="ssh"
            :device-ip="detailEntry.ip_address || ''"
            :ticket="guestTerminalTicket"
            ws-path="/ws/pve-terminal"
          />
          <Terminal
            v-else
            :key="`${detailEntry.device_id}-${detailContainer.name}`"
            minimal
            :device-id="detailEntry.device_id"
            conn-type="ssh"
            :device-ip="detailEntry.ip_address || ''"
            :credential-id="detailEntry.credential_id"
            :container="detailContainer.name"
          />
        </div>
      </div>
    </el-drawer>

    <!-- 容器日志弹窗 -->
    <el-dialog
      v-model="logsVisible"
      :title="`容器日志 — ${logsContainer}`"
      width="820px"
      top="6vh"
      append-to-body
      @close="onLogsDialogClose"
    >
      <div class="logs-toolbar">
        <el-select v-model="logsTail" class="logs-tail" @change="fetchLogs()">
          <el-option :value="100" label="最近 100 行" />
          <el-option :value="300" label="最近 300 行" />
          <el-option :value="1000" label="最近 1000 行" />
          <el-option :value="3000" label="最近 3000 行" />
        </el-select>
        <el-input
          v-model="logsFilter"
          placeholder="筛选日志(包含关键词)"
          prefix-icon="Search"
          clearable
          class="logs-filter"
        />
        <el-checkbox v-model="logsAuto">自动刷新(5s)</el-checkbox>
        <div class="logs-toolbar-right">
          <el-tag
            v-if="logsStaleAt"
            size="small"
            type="warning"
            effect="plain"
            class="logs-stale"
            title="最近一次自动刷新失败，当前显示的可能是过期日志"
          >
            刷新失败 {{ logsStaleAt }}
          </el-tag>
          <span class="logs-count">{{ logsLineCount }} 行</span>
          <el-button size="small" @click="copyLogs">复制</el-button>
          <el-button size="small" :loading="logsLoading" @click="fetchLogs()">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
        </div>
      </div>
      <pre v-loading="logsLoading" class="logs-viewer">{{ filteredLogs || '（无日志）' }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, WarningFilled, Connection, Document, Monitor, ArrowRight } from '@element-plus/icons-vue'
import { containersAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useMetricColor } from '@/composables/useMetricColor'
import { isWindowsOs } from '@/utils/osType'
const Terminal = defineAsyncComponent(() => import('@/components/terminal/Terminal.vue'))
import type { ContainerItem, DeviceContainersEntry } from '@/types/containers'

const authStore = useAuthStore()
// 容器控制不再单独占一个权限键：普通设备走 device:manage，虚拟机走 pve:manage。
const canControl = computed(() => authStore.hasPermission('device:manage') || authStore.hasPermission('pve:manage'))
const canTerminal = computed(() => authStore.hasPermission('device:remote'))
const canPveTerminal = computed(() => authStore.hasPermission('pve:manage'))
// 容器终端走 SSH 的 docker exec,Windows 设备/虚机约定不走 SSH,故不提供交互式终端。
// 设备目标走 device:remote(与设备终端同口径);PVE 虚机走 pve:manage + pve-terminal 链路。
const canContainerTerminal = computed(() => {
  const e = detailEntry.value
  if (!e) return false
  if (isWindowsOs(e.os_system)) return false
  return e.target_type === 'device' ? canTerminal.value : canPveTerminal.value
})

const loading = ref(false)
const entries = ref<DeviceContainersEntry[]>([])
const keyword = ref('')
const stateFilter = ref('')
const lastLoaded = ref<string | null>(null)
const actingKey = ref<string | null>(null)
const pollError = ref('')

let pollTimer: number | undefined
const pendingTimers: number[] = []
let loadInFlight = false

// ── 详情抽屉 ──
const detailVisible = ref(false)
const detailEntry = ref<DeviceContainersEntry | null>(null)
const detailContainer = ref<ContainerItem | null>(null)
const showTerminal = ref(false)
// PVE 虚机容器终端的一次性票据(开票后才能挂载 Terminal;设备目标由 Terminal 自行开票)
const guestTerminalTicket = ref('')
const terminalLoading = ref(false)

function openDetail(e: DeviceContainersEntry, c: ContainerItem) {
  detailEntry.value = e
  detailContainer.value = c
  showTerminal.value = false
  guestTerminalTicket.value = ''
  detailVisible.value = true
}

function closeTerminal() {
  showTerminal.value = false
  guestTerminalTicket.value = ''
}

// 「进入终端」:设备目标直接挂载 Terminal(它自己去 /api/terminal/ticket 开票);
// PVE 虚机先经 /api/containers/{id}/terminal-ticket 开票(后端从 binding 解析凭据),
// 拿到票据再挂载,连 /ws/pve-terminal 并自动 docker exec 进容器。
async function toggleTerminal() {
  if (showTerminal.value) {
    closeTerminal()
    return
  }
  const e = detailEntry.value
  const c = detailContainer.value
  if (!e || !c) return
  if (e.target_type === 'pve_guest') {
    if (guestTerminalTicket.value) {
      showTerminal.value = true
      return
    }
    terminalLoading.value = true
    try {
      const res = await containersAPI.terminalTicket(e.device_id, c.name)
      guestTerminalTicket.value = res.data.ticket
      showTerminal.value = true
    } catch (err: unknown) {
      const er = err as { response?: { data?: { detail?: string } } }
      ElMessage.error(er.response?.data?.detail || '获取容器终端票据失败')
    } finally {
      terminalLoading.value = false
    }
  } else {
    showTerminal.value = true
  }
}

// ── 端口映射解析:docker 原始格式 → 逐条映射(去重 IPv4/IPv6 重复项) ──
interface PortMap {
  hostIP: string
  hostPort: string
  containerPort: string
  proto: string
}

function parsePorts(ports: string | null): PortMap[] {
  if (!ports) return []
  const seen = new Set<string>()
  const out: PortMap[] = []
  for (const entryRaw of ports.split(',')) {
    const entry = entryRaw.trim()
    if (!entry) continue
    const arrow = entry.split('->')
    let hostIP = ''
    let hostPort = ''
    let containerPort: string
    let proto: string
    if (arrow.length === 2) {
      // 已发布: "0.0.0.0:8080" / ":::8080" -> "80/tcp"
      const hostPart = arrow[0]
      const cp = arrow[1].split('/')
      containerPort = cp[0]
      proto = cp[1] || 'tcp'
      const lastColon = hostPart.lastIndexOf(':')
      if (lastColon >= 0) {
        hostIP = hostPart.slice(0, lastColon)
        hostPort = hostPart.slice(lastColon + 1)
      }
    } else {
      // 仅暴露未发布: "80/tcp"
      const cp = entry.split('/')
      containerPort = cp[0]
      proto = cp[1] || 'tcp'
    }
    // 以 宿主机端口|容器端口|协议 去重(:::PORT 与 0.0.0.0:PORT 视为同一条)
    const key = `${hostPort}|${containerPort}|${proto}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ hostIP, hostPort, containerPort, proto })
  }
  return out
}

function fmtPortRow(p: PortMap): string {
  if (!p.hostPort) return `${p.containerPort}/${p.proto}`
  const host =
    p.hostIP && p.hostIP !== '0.0.0.0' && p.hostIP !== '::' && p.hostIP !== ''
      ? `${p.hostIP}:${p.hostPort}`
      : p.hostPort
  return `${host} → ${p.containerPort}/${p.proto}`
}

// 卡片上的紧凑端口文本
function compactPorts(ports: string | null): string {
  const list = parsePorts(ports)
  if (list.length === 0) return ''
  return list.map((p) => (p.hostPort ? `${p.hostPort}→${p.containerPort}` : `${p.containerPort}`)).join('，')
}

const detailPorts = computed<PortMap[]>(() => (detailContainer.value ? parsePorts(detailContainer.value.ports) : []))

// ── 汇总 ──
const summary = computed(() => {
  let total = 0
  let running = 0
  let stopped = 0
  let dockerHosts = 0
  for (const e of entries.value) {
    if (e.available) {
      dockerHosts++
      total += e.containers.length
      for (const c of e.containers) {
        if (c.state === 'running') running++
        else stopped++
      }
    }
  }
  return { total, running, stopped, dockerHosts }
})

const filteredEntries = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  // 无 Docker 的服务器/主机不展示(available 由采集器判定)
  // 普通设备没有 Docker 时隐藏；PVE Guest 只有实际发现容器后才显示。
  let list = entries.value.filter((e) =>
    e.target_type === 'pve_guest' ? e.available && e.container_count > 0 : e.available,
  )
  if (!q) return list
  return list.filter(
    (e) =>
      e.device_name.toLowerCase().includes(q) ||
      (e.ip_address || '').toLowerCase().includes(q) ||
      e.containers.some((c) => c.name.toLowerCase().includes(q) || (c.image || '').toLowerCase().includes(q)),
  )
})

function filterContainers(containers: ContainerItem[]): ContainerItem[] {
  const q = keyword.value.trim().toLowerCase()
  let list = containers
  if (stateFilter.value) list = list.filter((c) => c.state === stateFilter.value)
  if (q) {
    list = list.filter((c) => c.name.toLowerCase().includes(q) || (c.image || '').toLowerCase().includes(q))
  }
  return list
}

onMounted(() => {
  void load(false)
  pollTimer = window.setInterval(() => {
    if (!document.hidden) void load(true)
  }, 20000)
})

onUnmounted(() => {
  window.clearInterval(pollTimer)
  window.clearInterval(logsTimer)
  // 组件卸载后,setTimeout 排队的 load() 仍会命中已卸载组件,全部清掉。
  pendingTimers.forEach((t) => window.clearTimeout(t))
  pendingTimers.length = 0
})

/** 登记一个延迟 load,随组件卸载一并清理。 */
function scheduleLoad(delay: number) {
  pendingTimers.push(window.setTimeout(() => void load(), delay))
}

async function load(quiet = false) {
  if (loadInFlight) return
  loadInFlight = true
  if (!quiet) loading.value = true
  try {
    const res = await containersAPI.list()
    entries.value = res.data.items
    lastLoaded.value = new Date().toISOString()
    pollError.value = ''
    syncDetailFromEntries()
  } catch {
    if (!quiet) ElMessage.error('加载容器数据失败')
    pollError.value = '容器数据刷新失败，当前仍显示上一次成功数据，请手动重试'
  } finally {
    if (!quiet) loading.value = false
    loadInFlight = false
  }
}
function isStale(ts: string | null, maxAgeSeconds = 120): boolean {
  return !!ts && Date.now() - new Date(ts).getTime() > maxAgeSeconds * 1000
}

// 轮询刷新后,让打开中的详情抽屉同步最新容器状态
function syncDetailFromEntries() {
  if (!detailVisible.value || !detailEntry.value || !detailContainer.value) return
  const e = entries.value.find((x) => x.device_id === detailEntry.value!.device_id)
  if (!e) return
  const c = e.containers.find((x) => x.name === detailContainer.value!.name)
  if (c) {
    detailEntry.value = e
    detailContainer.value = c
  }
}

function key(deviceId: number, name: string, action: string): string {
  return `${deviceId}:${name}:${action}`
}

async function control(entry: DeviceContainersEntry, row: ContainerItem, action: string) {
  const actionLabel = action === 'start' ? '启动' : action === 'stop' ? '停止' : '重启'
  try {
    await ElMessageBox.confirm(`确定${actionLabel}容器「${row.name}」?`, `${actionLabel}确认`, {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }

  actingKey.value = key(entry.device_id, row.name, action)
  try {
    await containersAPI.control(entry.device_id, action, row.name)
    ElMessage.success(`已${actionLabel}容器「${row.name}」`)
    scheduleLoad(800)
    scheduleLoad(3000)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || `${actionLabel}失败`)
  } finally {
    actingKey.value = null
  }
}

// 删除容器:区分「是否保留数据」。匿名卷跟着容器走(-v 删除);具名卷和
// bind mount 两个选项下都保留。默认勾选保留——删错数据救不回来。
async function confirmRemove(entry: DeviceContainersEntry, row: ContainerItem) {
  let keepVolumes = true
  try {
    await ElMessageBox.confirm(`将删除容器「${row.name}」(运行中会先停止)。是否保留其数据?`, '删除容器', {
      distinguishCancelAndClose: true,
      confirmButtonText: '删除并保留数据',
      cancelButtonText: '删除并清空数据',
      closeOnClickModal: false,
      type: 'warning',
    })
    // resolve(主按钮「保留」)沿用初始值 true
  } catch (action: unknown) {
    // cancel = 「删除并清空数据」;close(右上角×/ESC) = 放弃操作
    if (action === 'close') return
    keepVolumes = false
  }

  actingKey.value = key(entry.device_id, row.name, 'remove')
  try {
    await containersAPI.control(entry.device_id, 'remove', row.name, keepVolumes)
    ElMessage.success(
      keepVolumes ? `已删除容器「${row.name}」(匿名卷已保留)` : `已删除容器「${row.name}」(匿名卷已删除)`,
    )
    scheduleLoad(800)
    scheduleLoad(3000)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '删除失败')
  } finally {
    actingKey.value = null
    // 容器已删,详情抽屉的数据随之失效,直接关掉
    detailVisible.value = false
    closeTerminal()
  }
}

// ── 容器日志 ──
const logsVisible = ref(false)
const logsDevice = ref<DeviceContainersEntry | null>(null)
const logsContainer = ref('')
const logsText = ref('')
const logsLoading = ref(false)
const logsTail = ref(100)
const logsFilter = ref('')
// 默认打开自动刷新：日志弹窗最常见的用途就是盯着实时输出。
const logsAuto = ref(true)
let logsTimer: number | undefined

// 静默(自动刷新)失败不打断阅读,但必须留痕:否则排障时盯着的是过期输出而不自知
const logsStaleAt = ref<string | null>(null)

async function fetchLogs(quiet = false) {
  if (!logsDevice.value || !logsContainer.value) return
  // 自动刷新走静默模式，否则每 5s 盖一层 loading 遮罩，根本没法阅读日志。
  if (!quiet) logsLoading.value = true
  try {
    const res = await containersAPI.logs(logsDevice.value.device_id, logsContainer.value, logsTail.value)
    logsText.value = res.data.log
    logsStaleAt.value = null
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    if (!quiet) {
      logsText.value = ''
      ElMessage.error(err.response?.data?.detail || '获取日志失败')
    } else {
      logsStaleAt.value = fmtTime(new Date().toISOString())
    }
  } finally {
    if (!quiet) logsLoading.value = false
  }
}

const filteredLogs = computed(() => {
  const lines = logsText.value.split('\n')
  const q = logsFilter.value.trim().toLowerCase()
  const out = q ? lines.filter((l) => l.toLowerCase().includes(q)) : lines
  return out.join('\n')
})

const logsLineCount = computed(() => (filteredLogs.value ? filteredLogs.value.split('\n').length : 0))

function openLogs(e: DeviceContainersEntry, c: ContainerItem) {
  logsDevice.value = e
  logsContainer.value = c.name
  logsText.value = ''
  logsFilter.value = ''
  logsStaleAt.value = null
  logsVisible.value = true
  fetchLogs()
}

function onLogsDialogClose() {
  window.clearInterval(logsTimer)
  logsTimer = undefined
}

watch([logsAuto, logsVisible], ([auto, vis]) => {
  window.clearInterval(logsTimer)
  logsTimer = undefined
  if (auto && vis) {
    logsTimer = window.setInterval(() => {
      // 页面切到后台时暂停拉取，避免无意义的 SSH/WinRM 往返。
      if (!document.hidden) void fetchLogs(true)
    }, 5000)
  }
})

async function copyLogs() {
  try {
    await navigator.clipboard.writeText(logsText.value)
    ElMessage.success('已复制日志')
  } catch {
    ElMessage.error('复制失败')
  }
}

function stateLabel(state: string | null): string {
  const map: Record<string, string> = {
    running: '运行中',
    exited: '已停止',
    created: '已创建',
    paused: '已暂停',
    restarting: '重启中',
    removing: '移除中',
    dead: '异常',
  }
  return map[state || ''] || state || '未知'
}

function stateTagType(state: string | null): string {
  if (state === 'running') return 'success'
  if (state === 'exited' || state === 'dead') return 'danger'
  if (state === 'paused' || state === 'restarting') return 'warning'
  return 'info'
}

function stateAccent(state: string | null): string {
  if (state === 'running') return 'accent-running'
  if (state === 'exited' || state === 'dead') return 'accent-stopped'
  return 'accent-other'
}

// 容器的 CPU/内存百分比是相对 limit 的占用率,与物理机巡检语义不完全等价;这里复用同一套
// 主机阈值只是近似(容器逼近 limit 同样意味着 OOM / 限流风险),好处是四个页面配色口径统一,
// 后端将来若下发容器专属阈值,只需扩 ThresholdMetric 并在这里换个 key。
const metricColor = useMetricColor()
function progressPct(p: number | null): number {
  return p == null ? 0 : Math.min(100, Math.max(0, p))
}

function fmtTime(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  const pad = (n: number) => (n < 10 ? `0${n}` : String(n))
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtMb(mb: number | null): string {
  if (mb == null) return '-'
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}GB` : `${mb}MB`
}
</script>

<style scoped>
.ctr-desc {
  font-size: 13px;
  color: var(--dcn-text-secondary);
}

.ctr-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-4) var(--dcn-space-6);
}

/* ── 汇总条 ── */
.ctr-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.sum-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: var(--dcn-space-3) var(--dcn-space-4);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
}
.sum-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--dcn-text-primary);
  font-variant-numeric: tabular-nums;
}
.sum-running {
  color: var(--dcn-dot-online);
}
.sum-stopped {
  color: var(--dcn-text-secondary);
}
.sum-label {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

/* ── 工具栏 ── */
.ctr-toolbar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  flex-wrap: wrap;
  padding: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
}
.ctr-search {
  width: 260px;
}
.ctr-filter {
  width: 140px;
}
.ctr-toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}
.ctr-updated {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

/* ── 设备分组 ── */
.ctr-device {
  margin-bottom: var(--dcn-space-5);
}
.ctr-device-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-2) var(--dcn-space-1);
  margin-bottom: var(--dcn-space-2);
  border-bottom: 1px solid var(--dcn-border);
}
.ctr-device-title {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  flex-wrap: wrap;
}
.ctr-device-name {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
}
.ctr-device-ip {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
}
.ctr-err-icon {
  color: var(--dcn-warning);
  font-size: 18px;
  cursor: help;
}

/* ── 容器卡片网格 ── */
.ctr-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--dcn-space-3);
}

.c-card {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-3);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  cursor: pointer;
  transition:
    box-shadow var(--dcn-transition-fast),
    transform var(--dcn-transition-fast),
    border-color var(--dcn-transition-fast);
}
.c-card:hover {
  box-shadow: var(--dcn-shadow-md);
  transform: translateY(-1px);
  border-color: var(--dcn-border-strong);
}
.c-card:focus-visible {
  outline: 2px solid var(--dcn-primary);
  outline-offset: 2px;
}

@media (max-width: 900px) {
  .ctr-body {
    padding: var(--dcn-space-4);
  }
  .ctr-summary {
    grid-template-columns: repeat(2, 1fr);
  }
  .ctr-toolbar-right {
    width: 100%;
    margin-left: 0;
    justify-content: space-between;
  }
}
.c-top {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.c-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.c-dot.accent-running {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 6px var(--dcn-dot-online);
}
.c-dot.accent-stopped {
  background: var(--dcn-dot-offline);
}
.c-dot.accent-other {
  background: var(--dcn-dot-maintenance);
}
.c-name {
  flex: 1;
  font-weight: 600;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.c-state {
  flex-shrink: 0;
}

.c-image {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.c-metrics {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-1);
}
.c-metric {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.c-metric-label {
  width: 30px;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  flex-shrink: 0;
}
.c-bar {
  flex: 1;
}
.c-metric-empty {
  flex: 1;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}
.c-metric-val {
  width: 52px;
  text-align: right;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

.c-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-2);
  padding-top: var(--dcn-space-1);
  border-top: 1px solid var(--dcn-border);
}
.c-ports {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}
.c-more {
  display: flex;
  align-items: center;
  gap: 2px;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  flex-shrink: 0;
}
.c-card:hover .c-more {
  color: var(--dcn-primary);
}

.ctr-nodata-card {
  padding: var(--dcn-space-4);
  text-align: center;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-sm);
  background: var(--dcn-bg-section);
  border: 1px dashed var(--dcn-border);
  border-radius: var(--dcn-radius-md);
}

/* ── 详情抽屉 ── */
.d-body {
  padding: 0 var(--dcn-space-1);
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
}
.d-status-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.d-image {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.d-mono {
  font-family: var(--dcn-font-mono);
}
.d-ports {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.d-port-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.d-port-icon {
  color: var(--dcn-text-placeholder);
  flex-shrink: 0;
}
.d-port-text {
  font-family: var(--dcn-font-mono);
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-primary);
}
.d-ports-empty {
  color: var(--dcn-text-placeholder);
}
.d-section-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin-top: var(--dcn-space-1);
}
.d-metrics {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}
.d-metric-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.d-metric-label {
  width: 40px;
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  flex-shrink: 0;
}
.d-bar {
  flex: 1;
}
.d-metric-none {
  flex: 1;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}
.d-mem-detail {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  padding-left: 48px;
}
.d-actions {
  display: flex;
  gap: var(--dcn-space-2);
  flex-wrap: wrap;
}
.d-terminal {
  height: 420px;
  margin-top: var(--dcn-space-3);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
  background: var(--dcn-bg-section);
}

/* ── 日志弹窗 ── */
.logs-toolbar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  flex-wrap: wrap;
  margin-bottom: var(--dcn-space-3);
}
.logs-tail {
  width: 150px;
}
.logs-filter {
  flex: 1;
  min-width: 180px;
}
.logs-toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.logs-count {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  font-variant-numeric: tabular-nums;
}
.logs-stale {
  font-variant-numeric: tabular-nums;
}
.logs-viewer {
  background: var(--dcn-bg-section);
  color: var(--dcn-text-regular);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  padding: var(--dcn-space-3);
  font-family: var(--dcn-font-mono);
  font-size: 12px;
  line-height: 1.5;
  max-height: 62vh;
  overflow: auto;
  white-space: pre;
  margin: 0;
}
</style>
