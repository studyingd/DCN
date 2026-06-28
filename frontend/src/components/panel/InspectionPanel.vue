<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">巡检管理</h2>
      <span style="font-size: 13px; color: var(--dcn-text-secondary);">设备健康巡检 · 网络设备 / Linux / Windows</span>
    </div>
    <div class="inline-panel-body">

    <el-tabs v-model="activeTab">
      <!-- ====== Tab 1: 即时巡检 ====== -->
      <el-tab-pane label="即时巡检" name="instant">
        <div class="section">
          <div class="section-header">
            <span class="section-title">选择设备</span>
            <span v-if="checkedDevices.length > 0" class="selected-count">
              已选 {{ checkedDevices.length }} 台
            </span>
          </div>
          <div class="cascade-row">
            <el-select v-model="selectedRoomId" placeholder="选择机房" clearable style="width: 180px;" @change="onRoomChange">
              <el-option v-for="r in rooms" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
            <el-select v-model="selectedRackId" placeholder="选择机柜" clearable style="width: 180px;" :disabled="!selectedRoomId" @change="onRackChange">
              <el-option v-for="rk in racks" :key="rk.id" :label="rk.name" :value="rk.id" />
            </el-select>
            <el-button type="primary" plain size="small" :disabled="!selectedRackId" @click="loadDevices">刷新</el-button>
          </div>

          <el-table
            ref="deviceTableRef"
            :data="devices"
            size="small"
            stripe
            max-height="240"
            @selection-change="onSelectionChange"
            v-loading="devicesLoading"
            v-if="devices.length > 0 || devicesLoading"
          >
            <el-table-column type="selection" width="40" />
            <el-table-column prop="name" label="设备名称" min-width="130" show-overflow-tooltip />
            <el-table-column prop="ip_address" label="IP 地址" width="140">
              <template #default="{ row }">{{ row.ip_address || '-' }}</template>
            </el-table-column>
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="targetTypeTag[row.target_type] || 'info'">
                  {{ targetTypeLabel[row.target_type] || row.target_type }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="70" align="center">
              <template #default="{ row }">
                <span class="status-dot" :class="'status-' + row.status" />
              </template>
            </el-table-column>
          </el-table>
          <div v-else-if="selectedRackId && !devicesLoading" class="empty-hint">当前机柜下无设备</div>
        </div>

        <!-- Mode selector -->
        <div class="section">
          <div class="section-header"><span class="section-title">巡检模式</span></div>
          <el-radio-group v-model="inspMode" size="small">
            <el-radio-button value="quick">快速 (5项)</el-radio-button>
            <el-radio-button value="standard">标准 (全量)</el-radio-button>
            <el-radio-button value="custom">自定义</el-radio-button>
          </el-radio-group>

          <div v-if="inspMode === 'custom'" class="custom-items" v-loading="catalogLoading">
            <el-checkbox-group v-model="customItems">
              <el-checkbox
                v-for="item in itemCatalog"
                :key="item.type"
                :value="item.type"
                :label="item.type"
              >
                {{ item.label }}
                <el-tag v-if="item.quick" size="small" type="success" style="margin-left:4px;">快速</el-tag>
              </el-checkbox>
            </el-checkbox-group>
          </div>
        </div>

        <!-- Run button -->
        <div class="section run-bar">
          <el-button
            type="primary"
            :icon="CaretRight"
            :loading="running"
            :disabled="checkedDevices.length === 0"
            @click="runInspection"
          >
            {{ running ? '巡检中...' : '开始巡检' }}
          </el-button>
          <span v-if="checkedDevices.length > 0" class="run-info">
            将巡检 {{ checkedDevices.length }} 台设备，
            每台约 {{ inspMode === 'quick' ? '5' : '13' }} 项
          </span>
        </div>

        <!-- Results -->
        <div v-if="runResults.length > 0" class="section">
          <div class="section-header">
            <span class="section-title">巡检结果</span>
            <span class="result-summary">
              共 {{ runResults.length }} 台，
              成功 {{ runSucceeded }} 台，
              异常 {{ runResults.length - runSucceeded }} 台
            </span>
          </div>
          <InspectionResultCard
            v-for="(r, idx) in runResults"
            :key="r.device_id"
            :result="r"
            :default-expanded="idx === 0"
          />
        </div>
      </el-tab-pane>

      <!-- ====== Tab 2: 巡检历史 ====== -->
      <el-tab-pane label="巡检历史" name="history">
        <div class="filter-row">
          <el-date-picker
            v-model="historyDateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYY-MM-DD"
            size="small"
            style="width: 260px;"
          />
          <el-select v-model="historyTargetType" placeholder="设备类型" clearable size="small" style="width: 120px;">
            <el-option label="网络设备" value="network" />
            <el-option label="Linux" value="linux" />
            <el-option label="Windows" value="windows" />
          </el-select>
          <el-select v-model="historyStatus" placeholder="状态" clearable size="small" style="width: 100px;">
            <el-option label="正常" value="completed" />
            <el-option label="部分异常" value="partial" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-button type="primary" size="small" @click="loadHistory">查询</el-button>
        </div>

        <el-table :data="historyItems" stripe size="small" v-loading="historyLoading">
          <el-table-column prop="device_name" label="设备" min-width="120" show-overflow-tooltip />
          <el-table-column prop="device_ip" label="IP" width="130">
            <template #default="{ row }">{{ row.device_ip || '-' }}</template>
          </el-table-column>
          <el-table-column label="类型" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="targetTypeTag[row.target_type] || 'info'">
                {{ targetTypeLabel[row.target_type] || row.target_type }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="mode" label="模式" width="70" align="center">
            <template #default="{ row }">{{ modeLabels[row.mode] || row.mode }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="recordStatusTag[row.status] || 'info'" size="small">
                {{ recordStatusLabel[row.status] || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="检查结果" width="160" align="center">
            <template #default="{ row }">
              <span class="mini-counts">
                <span class="mc-normal">✓{{ row.normal_count }}</span>
                <span v-if="row.warning_count" class="mc-warn">⚠{{ row.warning_count }}</span>
                <span v-if="row.critical_count" class="mc-crit">✕{{ row.critical_count }}</span>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="70" align="center">
            <template #default="{ row }">{{ row.duration_ms ? (row.duration_ms / 1000).toFixed(1) + 's' : '-' }}</template>
          </el-table-column>
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" align="center">
            <template #default="{ row }">
              <el-button type="primary" link size="small" @click="viewDetail(row.id)">详情</el-button>
              <el-button type="danger" link size="small" @click="deleteRecord(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pagination-row" v-if="historyTotal > historyPageSize">
          <el-pagination
            v-model:current-page="historyPage"
            :page-size="historyPageSize"
            :total="historyTotal"
            layout="prev, pager, next"
            size="small"
            @current-change="loadHistory"
          />
        </div>
      </el-tab-pane>

      <!-- ====== Tab 3: 巡检报告 ====== -->
      <el-tab-pane label="巡检报告" name="report">
        <div class="filter-row">
          <el-date-picker
            v-model="reportDateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            value-format="YYYY-MM-DD"
            size="small"
            style="width: 260px;"
          />
          <el-select v-model="reportTargetType" placeholder="设备类型" clearable size="small" style="width: 120px;">
            <el-option label="网络设备" value="network" />
            <el-option label="Linux" value="linux" />
            <el-option label="Windows" value="windows" />
          </el-select>
          <el-button type="primary" size="small" @click="loadReport">查询</el-button>
        </div>

        <div v-if="report" v-loading="reportLoading">
          <!-- Summary cards -->
          <div class="report-summary">
            <div class="report-card">
              <div class="report-card-value">{{ report.total_inspections }}</div>
              <div class="report-card-label">总巡检次数</div>
            </div>
            <div class="report-card">
              <div class="report-card-value">{{ report.overall_health.normal_pct }}%</div>
              <div class="report-card-label">健康率</div>
            </div>
            <div class="report-card">
              <div class="report-card-value" style="color: var(--el-color-warning);">{{ report.overall_health.warning_pct }}%</div>
              <div class="report-card-label">警告率</div>
            </div>
            <div class="report-card">
              <div class="report-card-value" style="color: var(--el-color-danger);">{{ report.overall_health.critical_pct }}%</div>
              <div class="report-card-label">严重率</div>
            </div>
          </div>

          <!-- By target type -->
          <div class="section" v-if="Object.keys(report.by_target_type).length > 0">
            <div class="section-header"><span class="section-title">按设备类型分布</span></div>
            <el-descriptions :column="3" border size="small">
              <el-descriptions-item v-for="(info, type) in report.by_target_type" :key="type" :label="targetTypeLabel[type] || type">
                {{ info.count }} 次巡检 · 告警率 {{ info.warning_rate }}% · 严重率 {{ info.critical_rate }}%
              </el-descriptions-item>
            </el-descriptions>
          </div>

          <!-- Top warnings -->
          <div class="section" v-if="report.top_warnings.length > 0">
            <div class="section-header"><span class="section-title">近期告警设备</span></div>
            <el-table :data="report.top_warnings" size="small" stripe>
              <el-table-column prop="device_name" label="设备" min-width="120" />
              <el-table-column label="类型" width="90">
                <template #default="{ row }">{{ targetTypeLabel[row.target_type] || row.target_type }}</template>
              </el-table-column>
              <el-table-column label="警告" width="70" align="center">
                <template #default="{ row }">
                  <span style="color: var(--el-color-warning);">{{ row.warning_count }}</span>
                </template>
              </el-table-column>
              <el-table-column label="严重" width="70" align="center">
                <template #default="{ row }">
                  <span style="color: var(--el-color-danger);">{{ row.critical_count }}</span>
                </template>
              </el-table-column>
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ formatTime(row.timestamp) }}</template>
              </el-table-column>
            </el-table>
          </div>

          <!-- Empty state -->
          <el-empty v-if="report.total_inspections === 0" description="暂无巡检数据" :image-size="60" />
        </div>
      </el-tab-pane>
    </el-tabs>

    </div>
  </div>

  <!-- Detail dialog -->
  <el-dialog v-model="detailVisible" title="巡检详情" width="750px" destroy-on-close>
    <div v-if="detailData" v-loading="detailLoading">
      <div class="detail-header">
        <span class="detail-device">{{ detailData.device_name }}</span>
        <el-tag :type="recordStatusTag[detailData.status] || 'info'" size="small">{{ recordStatusLabel[detailData.status] || detailData.status }}</el-tag>
        <el-tag size="small" :type="targetTypeTag[detailData.target_type] || 'info'">
          {{ targetTypeLabel[detailData.target_type] || detailData.target_type }}
        </el-tag>
        <span class="detail-time">{{ formatTime(detailData.started_at) }}</span>
        <span class="detail-duration">{{ detailData.duration_ms ? (detailData.duration_ms / 1000).toFixed(1) + 's' : '' }}</span>
      </div>
      <div class="detail-counts">
        <el-tag type="success" effect="plain" size="small">正常 {{ detailData.normal_count }}</el-tag>
        <el-tag v-if="detailData.warning_count" type="warning" effect="plain" size="small">警告 {{ detailData.warning_count }}</el-tag>
        <el-tag v-if="detailData.critical_count" type="danger" effect="plain" size="small">严重 {{ detailData.critical_count }}</el-tag>
        <el-tag v-if="detailData.error_count" type="info" effect="plain" size="small">错误 {{ detailData.error_count }}</el-tag>
      </div>
      <el-table :data="detailData.items" size="small" stripe max-height="400">
        <el-table-column label="状态" width="60" align="center">
          <template #default="{ row }">
            <span class="status-dot" :class="'dot-' + row.status" />
          </template>
        </el-table-column>
        <el-table-column label="检查项" min-width="110">
          <template #default="{ row }">{{ getItemLabel(row.item_type) }}</template>
        </el-table-column>
        <el-table-column label="结果" width="120">
          <template #default="{ row }">
            <span v-if="row.value">{{ row.value }}</span>
            <span v-if="row.unit" style="color: var(--dcn-text-secondary); margin-left: 2px;">{{ row.unit }}</span>
          </template>
        </el-table-column>
        <el-table-column label="标签" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.status !== 'normal'" :type="itemStatusTag[row.status] || 'info'" size="small">
              {{ itemStatusLabel[row.status] || row.status }}
            </el-tag>
            <span v-else style="color: var(--el-color-success);">✓</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button v-if="row.raw_output" type="primary" link size="small" @click="showRaw(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </el-dialog>

  <!-- Raw output dialog -->
  <el-dialog v-model="rawVisible" title="原始输出" width="650px" destroy-on-close>
    <pre class="raw-pre">{{ rawContent }}</pre>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CaretRight } from '@element-plus/icons-vue'
import { roomAPI, rackAPI, deviceAPI, inspectionAPI } from '@/api'
import InspectionResultCard from './InspectionResultCard.vue'
import type { InspectionDeviceItem, InspectionItemInfo, InspectionDeviceResult, InspectionRecord, InspectionRecordDetail, InspectionReport } from '@/types/inspection'
import type { Room, Rack, Device } from '@/types'

const activeTab = ref('instant')

// ── Labels ──
const targetTypeLabel: Record<string, string> = { network: '网络设备', linux: 'Linux', windows: 'Windows' }
const targetTypeTag: Record<string, string> = { network: 'warning', linux: 'success', windows: 'primary' }
const modeLabels: Record<string, string> = { standard: '标准', quick: '快速', custom: '自定义' }
const recordStatusLabel: Record<string, string> = { completed: '正常', partial: '部分异常', failed: '失败', running: '运行中' }
const recordStatusTag: Record<string, string> = { completed: 'success', partial: 'warning', failed: 'danger', running: 'info' }
const itemStatusLabel: Record<string, string> = { warning: '警告', critical: '严重', error: '错误' }
const itemStatusTag: Record<string, string> = { warning: 'warning', critical: 'danger', error: 'info' }

const ITEM_LABELS: Record<string, string> = {
  cpu: 'CPU 使用率', memory: '内存使用', interface: '接口状态', version: '系统版本',
  routes: '路由表', log: '系统日志', environment: '环境温度', power: '电源状态',
  fan: '风扇状态', stp: 'STP 状态', vlan: 'VLAN', arp: 'ARP 表', mac: 'MAC 表',
  disk: '磁盘使用', load: '系统负载', network: '网络配置', ports: '监听端口',
  processes: '进程列表', os_version: '系统版本', logs: '异常日志',
  failed_services: '失败服务', firewall: '防火墙', security_updates: '安全更新',
  logins: '登录记录', system_info: '系统信息', services: '失败服务',
  event_logs: '事件日志', updates: '已装补丁', uptime: '运行时间',
}
function getItemLabel(type: string) { return ITEM_LABELS[type] || type }

function formatTime(ts: string | null | undefined): string {
  if (!ts) return '-'
  try {
    const d = new Date(ts)
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return ts }
}

// ═══════════════════════════════════════════
// Tab 1: 即时巡检
// ═══════════════════════════════════════════

const rooms = ref<Room[]>([])
const racks = ref<Rack[]>([])
const devices = ref<Device[]>([])
const selectedRoomId = ref<number | ''>('')
const selectedRackId = ref<number | ''>('')
const devicesLoading = ref(false)
const checkedDevices = ref<Device[]>([])
const deviceTableRef = ref()

async function onRoomChange(roomId: number | '') {
  selectedRackId.value = ''
  racks.value = []
  devices.value = []
  checkedDevices.value = []
  if (!roomId) return
  try {
    const res = await rackAPI.list(roomId)
    racks.value = res.data
  } catch { /* ignore */ }
}

async function onRackChange(_rackId: number | '') {
  devices.value = []
  checkedDevices.value = []
  if (_rackId) await loadDevices()
}

async function loadDevices() {
  if (!selectedRackId.value) return
  devicesLoading.value = true
  try {
    const res = await deviceAPI.list(selectedRackId.value as number)
    devices.value = res.data
  } catch { /* ignore */ } finally { devicesLoading.value = false }
}

function onSelectionChange(selection: Device[]) {
  checkedDevices.value = selection
}

// Mode
const inspMode = ref<'quick' | 'standard' | 'custom'>('quick')
const customItems = ref<string[]>([])
const itemCatalog = ref<InspectionItemInfo[]>([])
const catalogLoading = ref(false)

watch(inspMode, async (mode) => {
  if (mode === 'custom' && checkedDevices.value.length > 0) {
    await loadCatalog()
  }
})

watch(checkedDevices, async () => {
  if (inspMode.value === 'custom') await loadCatalog()
})

async function loadCatalog() {
  if (checkedDevices.value.length === 0) return
  // Use first device's target_type for catalog
  const firstDevice = checkedDevices.value[0]
  let targetType = 'linux'
  if (['switch', 'router', 'firewall'].includes(firstDevice.type)) targetType = 'network'
  else if ((firstDevice.os_system || '').toLowerCase().includes('windows')) targetType = 'windows'

  catalogLoading.value = true
  try {
    const res = await inspectionAPI.getItemsCatalog(targetType)
    itemCatalog.value = res.data.items
  } catch { /* ignore */ } finally { catalogLoading.value = false }
}

// Run
const running = ref(false)
const runResults = ref<InspectionDeviceResult[]>([])

const runSucceeded = computed(() => runResults.value.filter(r => r.status !== 'failed').length)

async function runInspection() {
  if (checkedDevices.value.length === 0) return

  if (inspMode.value === 'custom' && customItems.value.length === 0) {
    ElMessage.warning('请至少选择一个巡检项')
    return
  }

  running.value = true
  runResults.value = []
  try {
    const res = await inspectionAPI.run({
      device_ids: checkedDevices.value.map(d => d.id),
      mode: inspMode.value,
      items: inspMode.value === 'custom' ? customItems.value : undefined,
      timeout: 30,
    })
    runResults.value = res.data.results
    const failed = res.data.failed
    if (failed === 0) {
      ElMessage.success(`巡检完成：${res.data.succeeded} 台设备全部正常`)
    } else {
      ElMessage.warning(`巡检完成：${res.data.succeeded} 台正常，${failed} 台异常`)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '巡检执行失败')
  } finally {
    running.value = false
  }
}

// ═══════════════════════════════════════════
// Tab 2: 巡检历史
// ═══════════════════════════════════════════

const historyItems = ref<InspectionRecord[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyPageSize = 20
const historyLoading = ref(false)
const historyDateRange = ref<[string, string] | null>(null)
const historyTargetType = ref('')
const historyStatus = ref('')

async function loadHistory() {
  historyLoading.value = true
  try {
    const params: Record<string, any> = {
      page: historyPage.value,
      page_size: historyPageSize,
    }
    if (historyTargetType.value) params.target_type = historyTargetType.value
    if (historyStatus.value) params.status = historyStatus.value
    if (historyDateRange.value) {
      params.start_date = historyDateRange.value[0]
      params.end_date = historyDateRange.value[1]
    }
    const res = await inspectionAPI.listRecords(params)
    historyItems.value = res.data.items
    historyTotal.value = res.data.total
  } catch { /* ignore */ } finally { historyLoading.value = false }
}

// Detail dialog
const detailVisible = ref(false)
const detailData = ref<InspectionRecordDetail | null>(null)
const detailLoading = ref(false)

async function viewDetail(id: number) {
  detailVisible.value = true
  detailLoading.value = true
  try {
    const res = await inspectionAPI.getRecordDetail(id)
    detailData.value = res.data
  } catch {
    ElMessage.error('加载详情失败')
  } finally { detailLoading.value = false }
}

async function deleteRecord(row: InspectionRecord) {
  try {
    await ElMessageBox.confirm(`确定删除「${row.device_name}」的巡检记录？`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await inspectionAPI.deleteRecord(row.id)
    ElMessage.success('已删除')
    loadHistory()
  } catch { ElMessage.error('删除失败') }
}

// Raw output dialog
const rawVisible = ref(false)
const rawContent = ref('')

function showRaw(item: any) {
  rawContent.value = item.raw_output || '(无输出)'
  rawVisible.value = true
}

// ═══════════════════════════════════════════
// Tab 3: 巡检报告
// ═══════════════════════════════════════════

const report = ref<InspectionReport | null>(null)
const reportLoading = ref(false)
const reportDateRange = ref<[string, string] | null>(null)
const reportTargetType = ref('')

async function loadReport() {
  reportLoading.value = true
  try {
    const params: Record<string, any> = {}
    if (reportTargetType.value) params.target_type = reportTargetType.value
    if (reportDateRange.value) {
      params.start_date = reportDateRange.value[0]
      params.end_date = reportDateRange.value[1]
    }
    const res = await inspectionAPI.getReport(params)
    report.value = res.data
  } catch { /* ignore */ } finally { reportLoading.value = false }
}

// ── Init ──
onMounted(async () => {
  try {
    const res = await roomAPI.list()
    rooms.value = res.data
  } catch { /* ignore */ }
  loadHistory()
  loadReport()
})
</script>

<style scoped>
.inline-panel-header {
  display: flex; align-items: center; gap: 16px;
}
.section { margin-bottom: 20px; }
.section-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.section-title {
  font-size: 14px; font-weight: 600; color: var(--dcn-text-primary);
}
.selected-count {
  font-size: 12px; color: var(--dcn-primary);
}
.cascade-row {
  display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
}
.empty-hint {
  text-align: center; padding: 20px; color: var(--dcn-text-secondary); font-size: 13px;
}

.status-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
}
.status-dot.status-online { background: var(--el-color-success); }
.status-dot.status-offline { background: var(--dcn-text-secondary); }
.status-dot.status-maintenance { background: var(--el-color-warning); }
.dot-normal { background: var(--el-color-success); }
.dot-warning { background: var(--el-color-warning); }
.dot-critical { background: var(--el-color-danger); }
.dot-error { background: var(--dcn-text-secondary); }

.custom-items {
  margin-top: 10px; padding: 12px;
  background: var(--dcn-bg-section); border-radius: var(--dcn-radius-md);
}
.custom-items :deep(.el-checkbox) { margin-right: 16px; margin-bottom: 8px; }

.run-bar {
  display: flex; align-items: center; gap: 12px;
}
.run-info { font-size: 12px; color: var(--dcn-text-secondary); }

.result-summary { font-size: 12px; color: var(--dcn-text-secondary); }

.mini-counts { font-size: 12px; }
.mc-normal { color: var(--el-color-success); }
.mc-warn { color: var(--el-color-warning); margin-left: 6px; }
.mc-crit { color: var(--el-color-danger); margin-left: 6px; }

.filter-row {
  display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap;
}
.pagination-row {
  display: flex; justify-content: center; margin-top: 16px;
}

/* Report */
.report-summary {
  display: flex; gap: 16px; margin-bottom: 20px;
}
.report-card {
  flex: 1; text-align: center; padding: 16px;
  background: var(--dcn-bg-section); border-radius: var(--dcn-radius-lg);
  border: 1px solid var(--dcn-border);
}
.report-card-value {
  font-size: 28px; font-weight: 700; color: var(--dcn-text-primary);
  font-family: var(--dcn-font-mono);
}
.report-card-label {
  font-size: 12px; color: var(--dcn-text-secondary); margin-top: 4px;
}

/* Detail */
.detail-header {
  display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
}
.detail-device { font-weight: 600; font-size: 16px; color: var(--dcn-text-primary); }
.detail-time { font-size: 12px; color: var(--dcn-text-secondary); margin-left: auto; }
.detail-duration { font-size: 12px; color: var(--dcn-text-secondary); font-family: var(--dcn-font-mono); }
.detail-counts { display: flex; gap: 8px; margin-bottom: 16px; }

.raw-pre {
  background: var(--dcn-bg-section); border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md); padding: 12px;
  font-family: var(--dcn-font-mono); font-size: 12px;
  color: var(--dcn-text-regular); max-height: 400px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all; margin: 0;
}
</style>
