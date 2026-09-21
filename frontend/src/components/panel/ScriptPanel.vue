<template>
  <div class="script-panel" :class="{ 'script-panel-embedded': embedded }">
    <div v-if="!embedded" class="inline-panel-header">
      <h2 class="inline-panel-title">脚本管理</h2>
      <span class="script-panel-desc">批量选择设备并执行命令</span>
    </div>
    <div :class="embedded ? '' : 'inline-panel-body'">
      <el-tabs v-model="activeTab" class="script-tabs">
        <!-- ====== Tab: Instant Execution ====== -->
        <el-tab-pane label="即时执行" name="instant">
          <!-- Cascade Selection -->
          <div class="script-section">
            <div class="section-header">
              <span class="section-title">选择设备</span>
              <span v-if="selectedDevices.length > 0" class="selected-count">
                已选 {{ selectedDevices.length }} 台
              </span>
            </div>
            <div class="cascade-row">
              <el-select
                v-model="selectedRoomId"
                placeholder="选择机房"
                clearable
                style="width: 200px"
                @change="onRoomChange"
              >
                <el-option v-for="r in rooms" :key="r.id" :label="r.name" :value="r.id" />
              </el-select>
              <el-select
                v-model="selectedRackId"
                placeholder="选择机柜"
                clearable
                style="width: 200px"
                :disabled="!selectedRoomId"
                @change="onRackChange"
              >
                <el-option v-for="rk in racks" :key="rk.id" :label="rk.name" :value="rk.id" />
              </el-select>
              <el-button type="primary" plain size="small" :disabled="!selectedRackId" @click="loadDevices">
                刷新
              </el-button>
            </div>
            <el-table
              v-if="devices.length > 0 || devicesLoading"
              ref="deviceTableRef"
              v-loading="devicesLoading"
              :data="devices"
              size="small"
              stripe
              max-height="280"
              @selection-change="onSelectionChange"
            >
              <el-table-column type="selection" width="40" />
              <el-table-column prop="name" label="设备名称" min-width="140" show-overflow-tooltip />
              <el-table-column prop="ip_address" label="IP 地址" width="150">
                <template #default="{ row }">{{ row.ip_address || '-' }}</template>
              </el-table-column>
              <el-table-column prop="type" label="类型" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="typeTagMap[row.type] || 'info'">{{
                    typeLabels[row.type] || row.type
                  }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="80" align="center">
                <template #default="{ row }">
                  <span class="status-dot" :class="'status-' + row.status" />
                  <span class="status-text">{{ statusLabels[row.status] || row.status }}</span>
                </template>
              </el-table-column>
              <el-table-column label="凭据" width="70" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.credential_id" size="small" type="success">已绑定</el-tag>
                  <el-tag v-else size="small" type="info">未绑定</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="系统" width="80" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="osTagType(row)">{{ osLabel(row) }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div v-else-if="selectedRackId && !devicesLoading" class="empty-hint">当前机柜下无设备</div>
          </div>

          <!-- Command Input -->
          <div class="script-section">
            <div class="section-header">
              <span class="section-title">执行命令</span>
            </div>
            <div class="preset-bar">
              <span class="preset-label">快捷指令:</span>
              <el-button v-for="p in commandPresets" :key="p.label" size="small" plain @click="applyPreset(p)"
                >{{ p.label }}<span class="preset-os-tag">{{ presetOsTag(p) }}</span></el-button
              >
              <el-divider direction="vertical" class="preset-sep" />
              <el-button
                v-for="p in powerPresets"
                :key="p.action"
                size="small"
                :type="p.type"
                :loading="powering"
                :disabled="selectedDevices.length === 0"
                @click="executePower(p.action)"
              >
                <el-icon><component :is="p.icon" /></el-icon>
                {{ p.label }} ({{ selectedDevices.length }})
              </el-button>
            </div>
            <el-input
              v-model="command"
              type="textarea"
              :rows="4"
              placeholder="输入要在选中设备上执行的命令，如: hostname、df -h、free -m（Linux）或 Get-Volume（Windows PowerShell）"
              style="font-family: 'Cascadia Code', 'Fira Code', Menlo, monospace"
            />
            <div class="command-footer">
              <div class="timeout-control">
                <span>超时:</span>
                <el-input-number
                  v-model="timeout"
                  :controls="false"
                  :min="5"
                  :max="300"
                  size="small"
                  style="width: 120px"
                />
                <span>秒</span>
              </div>
              <el-button
                type="primary"
                :loading="executing"
                :disabled="selectedDevices.length === 0 || !command.trim()"
                @click="executeScript"
              >
                <el-icon><Promotion /></el-icon>
                执行命令 ({{ selectedDevices.length }} 台)
              </el-button>
            </div>
          </div>

          <!-- Results -->
          <div v-if="hasExecuted" class="script-section">
            <div class="section-header">
              <span class="section-title">{{ lastResultKind === 'power' ? '电源操作结果' : '执行结果' }}</span>
              <div class="result-summary">
                <el-tag type="info" size="small">总计 {{ results.length }}</el-tag>
                <el-tag type="success" size="small">成功 {{ succeeded }}</el-tag>
                <el-tag type="danger" size="small">失败 {{ failed }}</el-tag>
              </div>
            </div>
            <el-table :data="results" size="small" stripe row-key="device_id">
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="result-expand">
                    <div v-if="lastResultKind === 'power' && row.stdout" class="result-block">
                      <div class="result-label">消息:</div>
                      <pre class="result-pre">{{ row.stdout }}</pre>
                    </div>
                    <template v-else>
                      <div v-if="row.stdout" class="result-block">
                        <div class="result-label">stdout:</div>
                        <pre class="result-pre">{{ row.stdout }}</pre>
                      </div>
                      <div v-if="row.stderr" class="result-block">
                        <div class="result-label">stderr:</div>
                        <pre class="result-pre error">{{ row.stderr }}</pre>
                      </div>
                    </template>
                    <div v-if="row.error" class="result-block">
                      <div class="result-label">错误:</div>
                      <pre class="result-pre error">{{ row.error }}</pre>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="device_name" label="设备名称" min-width="140" show-overflow-tooltip />
              <el-table-column prop="ip_address" label="IP" width="140">
                <template #default="{ row }">{{ row.ip_address || '-' }}</template>
              </el-table-column>
              <el-table-column label="状态" width="80" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                    {{ row.success ? '成功' : '失败' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="lastResultKind !== 'power'" label="退出码" width="80" align="center">
                <template #default="{ row }">
                  <code v-if="row.exit_code !== null" :class="row.exit_code === 0 ? 'code-ok' : 'code-err'">{{
                    row.exit_code
                  }}</code>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column
                :label="lastResultKind === 'power' ? '执行消息' : '输出预览'"
                min-width="200"
                show-overflow-tooltip
              >
                <template #default="{ row }">{{ row.stdout || row.error || row.stderr || '-' }}</template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup lang="ts">
// embedded: 作为「自动化执行」面板的 tab 嵌入时,隐藏自带头部/让出布局控制
withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })
import { ref, computed, watch, onMounted } from 'vue'
import { Promotion, Refresh, SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { scriptAPI, roomAPI, rackAPI, deviceAPI } from '@/api'
import type { Room, Rack, Device } from '@/types'

interface ScriptResult {
  device_id: number
  device_name: string
  ip_address: string | null
  success: boolean
  exit_code: number | null
  stdout: string
  stderr: string
  error: string
}

const typeLabels: Record<string, string> = {
  server: '服务器',
  cloud_server: '云服务器',
  host: '台式主机',
}
const typeTagMap: Record<string, string> = {
  server: '',
  cloud_server: 'primary',
  host: 'info',
}
const statusLabels: Record<string, string> = {
  online: '在线',
  offline: '离线',
  maintenance: '离线',
}

// 命令模板：点击后填入命令输入框，根据选中设备 OS 自动选择命令
const commandPresets = [
  { label: '主机名', linux: 'hostname', windows: 'hostname' },
  { label: '磁盘', linux: 'df -h', windows: 'Get-Volume | Format-Table -AutoSize' },
  {
    label: '内存',
    linux: 'free -h',
    windows: 'Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory',
  },
  { label: '运行时间', linux: 'uptime', windows: '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime' },
]

// 电源操作：点击直接调用批量电源接口(Windows 走 WinRM,Linux 走 SSH,指令发出后探测离线确认)
const powerPresets = [
  { label: '关机', action: 'shutdown' as const, type: 'danger' as const, icon: SwitchButton },
  { label: '重启', action: 'reboot' as const, type: 'warning' as const, icon: Refresh },
]

function isWindows(device: Device): boolean {
  return (device.os_system || '').toLowerCase().includes('windows')
}

function selectedMajorityOS(): 'linux' | 'windows' {
  const win = selectedDevices.value.filter((d) => isWindows(d)).length
  const nix = selectedDevices.value.filter((d) => !isWindows(d)).length
  return win > nix ? 'windows' : 'linux'
}

function presetOsTag(_preset: (typeof commandPresets)[0]): string {
  if (selectedDevices.value.length === 0) return ''
  const os = selectedMajorityOS()
  return os === 'windows' ? ' (Win)' : ' (Linux)'
}

function applyPreset(preset: (typeof commandPresets)[0]) {
  const os = selectedDevices.value.length > 0 ? selectedMajorityOS() : 'linux'
  command.value = os === 'windows' ? preset.windows : preset.linux
}

function osLabel(device: Device): string {
  const os = (device.os_system || '').toLowerCase()
  if (os.includes('windows')) return 'Windows'
  return 'Linux'
}

function osTagType(device: Device): string {
  const os = (device.os_system || '').toLowerCase()
  if (os.includes('windows')) return 'warning'
  return 'success'
}

// ====== Tab state ======
const route = useRoute()
const router = useRouter()
const activeTab = ref((route.query.tab as string) || 'instant')

// ====== Instant execution state ======
const rooms = ref<Room[]>([])
const racks = ref<Rack[]>([])
const selectedRoomId = ref<number | null>(null)
const selectedRackId = ref<number | null>(null)

const devices = ref<Device[]>([])
const selectedDevices = ref<Device[]>([])
const devicesLoading = ref(false)

const command = ref('')
const timeout = ref(30)
const executing = ref(false)
const powering = ref(false)
const results = ref<ScriptResult[]>([])
const hasExecuted = ref(false)
const lastResultKind = ref<'script' | 'power' | null>(null)

const succeeded = computed(() => results.value.filter((r) => r.success).length)
const failed = computed(() => results.value.filter((r) => !r.success).length)

async function loadRooms() {
  try {
    const res = await roomAPI.list()
    rooms.value = res.data
  } catch {
    ElMessage.error('加载机房列表失败')
  }
}

async function onRoomChange(roomId: number | null) {
  selectedRackId.value = null
  racks.value = []
  devices.value = []
  selectedDevices.value = []
  if (!roomId) return
  try {
    const res = await rackAPI.list(roomId)
    racks.value = res.data
  } catch {
    ElMessage.error('加载机柜列表失败')
  }
}

function onRackChange(rackId: number | null) {
  devices.value = []
  selectedDevices.value = []
  if (rackId) loadDevices()
}

async function loadDevices() {
  if (!selectedRackId.value) return
  devicesLoading.value = true
  try {
    const res = await deviceAPI.list(selectedRackId.value)
    devices.value = res.data
  } catch {
    ElMessage.error('加载设备列表失败')
  } finally {
    devicesLoading.value = false
  }
}

function onSelectionChange(selection: Device[]) {
  selectedDevices.value = selection
}

async function executeScript() {
  if (selectedDevices.value.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  if (!command.value.trim()) {
    ElMessage.warning('请输入要执行的命令')
    return
  }

  try {
    const cmdPreview = command.value
    await ElMessageBox.confirm(`将在 ${selectedDevices.value.length} 台设备上执行命令:\n${cmdPreview}`, '确认执行', {
      confirmButtonText: '执行',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }

  const targetIds = selectedDevices.value.map((d) => d.id)
  executing.value = true
  hasExecuted.value = true
  lastResultKind.value = 'script'
  try {
    const res = await scriptAPI.execute({
      device_ids: targetIds,
      command: command.value,
      timeout: timeout.value,
    })
    results.value = res.data.results
    if (res.data.failed === 0) {
      ElMessage.success(`全部 ${res.data.succeeded} 台设备执行成功`)
    } else {
      ElMessage.warning(`执行完成: ${res.data.succeeded} 成功, ${res.data.failed} 失败`)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '执行失败')
  } finally {
    executing.value = false
  }
}

// ====== Batch power (shutdown / reboot) ======
async function executePower(action: 'shutdown' | 'reboot') {
  if (selectedDevices.value.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  const actionLabel = action === 'shutdown' ? '关机' : '重启'
  const devicesList = selectedDevices.value.map((d) => `  • ${d.name} (${d.ip_address || '-'})`).join('\n')
  try {
    await ElMessageBox.confirm(
      `将对以下 ${selectedDevices.value.length} 台设备执行【${actionLabel}】操作:\n${devicesList}\n\n此操作不可撤销，确认继续？`,
      `批量${actionLabel}确认`,
      { confirmButtonText: `确认${actionLabel}`, cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  const targetIds = selectedDevices.value.map((d) => d.id)
  powering.value = true
  hasExecuted.value = true
  lastResultKind.value = 'power'
  try {
    const res = await scriptAPI.power({ device_ids: targetIds, action })
    // 把电源结果转成 ScriptResult 形态，复用结果表格
    results.value = res.data.results.map((r: any) => ({
      device_id: r.device_id,
      device_name: r.device_name,
      ip_address: r.ip_address,
      success: r.success,
      exit_code: r.success ? 0 : 1,
      stdout: r.message || '',
      stderr: '',
      error: r.success ? '' : r.message || '',
    })) as ScriptResult[]
    if (res.data.failed === 0) {
      ElMessage.success(`全部 ${res.data.succeeded} 台设备${actionLabel}指令已发送`)
    } else {
      ElMessage.warning(`${actionLabel}完成: ${res.data.succeeded} 成功, ${res.data.failed} 失败`)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || `${actionLabel}失败`)
  } finally {
    powering.value = false
  }
}

watch(activeTab, (tab) => {
  router.replace({ query: { ...route.query, tab } })
})

onMounted(() => {
  loadRooms()
})
</script>

<style scoped>
.script-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.script-panel-embedded {
  height: auto;
  overflow: visible;
}

/* .inline-panel, .inline-panel-header, .inline-panel-title, .inline-panel-body defined globally in utilities.css */

.script-panel-desc {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
}

.script-tabs {
  margin-top: -8px;
}

.script-section {
  margin-bottom: var(--dcn-space-6);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.section-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.selected-count {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-primary);
}

.cascade-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-3);
}

.preset-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.preset-label {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  flex-shrink: 0;
}

.preset-bar .el-button {
  font-size: var(--dcn-text-sm);
  padding: 4px var(--dcn-space-2);
}

.preset-os-tag {
  font-size: 10px;
  opacity: 0.55;
  margin-left: 2px;
}

.preset-bar .preset-sep.el-divider--vertical {
  margin: 0 4px;
  height: 18px;
}

.unsupported-tip {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-warning);
}

.empty-hint {
  text-align: center;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-base);
  padding: var(--dcn-space-5) 0;
}

.command-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.timeout-control {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
}

.result-summary {
  display: flex;
  gap: 6px;
}

.result-expand {
  padding: var(--dcn-space-3) var(--dcn-space-4);
}

.result-block {
  margin-bottom: var(--dcn-space-2);
}

.result-block:last-child {
  margin-bottom: 0;
}

.result-label {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  margin-bottom: var(--dcn-space-1);
}

.result-pre {
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
  padding: var(--dcn-space-2) var(--dcn-space-3);
  font-size: var(--dcn-text-sm);
  font-family: var(--dcn-font-mono);
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  color: var(--dcn-text-primary);
  max-height: 300px;
  overflow-y: auto;
}

.result-pre.error {
  color: var(--dcn-danger);
  background: var(--dcn-danger-light);
  border-color: var(--dcn-danger-light);
}

.status-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: var(--dcn-radius-full);
  margin-right: 4px;
  vertical-align: middle;
}

.status-online {
  background: var(--dcn-dot-online);
}
.status-offline {
  background: var(--dcn-text-secondary);
}
.status-maintenance {
  background: var(--dcn-dot-maintenance);
}

.status-text {
  font-size: var(--dcn-text-sm);
}

.code-ok {
  color: var(--dcn-success);
  font-family: var(--dcn-font-mono);
}

.code-err {
  color: var(--dcn-danger);
  font-family: var(--dcn-font-mono);
}

/* ====== Form improvements ====== */

.field-hint {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  margin-top: var(--dcn-space-1);
  line-height: 1.4;
}

.schedule-radio-group .el-radio-button {
  flex: 1;
}

/* ====== Device select trigger ====== */

.device-tag :deep(.el-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ====== Device Picker Dialog ====== */
</style>
