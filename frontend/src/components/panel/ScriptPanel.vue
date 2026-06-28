<template>
  <div class="script-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">脚本管理</h2>
      <span class="script-panel-desc">批量选择设备并执行命令</span>
    </div>
    <div class="inline-panel-body">

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
              <el-select v-model="selectedRoomId" placeholder="选择机房" clearable style="width: 200px;" @change="onRoomChange">
                <el-option v-for="r in rooms" :key="r.id" :label="r.name" :value="r.id" />
              </el-select>
              <el-select v-model="selectedRackId" placeholder="选择机柜" clearable style="width: 200px;" :disabled="!selectedRoomId" @change="onRackChange">
                <el-option v-for="rk in racks" :key="rk.id" :label="rk.name" :value="rk.id" />
              </el-select>
              <el-button type="primary" plain size="small" :disabled="!selectedRackId" @click="loadDevices">
                刷新
              </el-button>
            </div>
            <el-table
              ref="deviceTableRef"
              :data="devices"
              size="small"
              stripe
              max-height="280"
              @selection-change="onSelectionChange"
              v-loading="devicesLoading"
              v-if="devices.length > 0 || devicesLoading"
            >
              <el-table-column type="selection" width="40" />
              <el-table-column prop="name" label="设备名称" min-width="140" show-overflow-tooltip />
              <el-table-column prop="ip_address" label="IP 地址" width="150">
                <template #default="{ row }">{{ row.ip_address || '-' }}</template>
              </el-table-column>
              <el-table-column prop="type" label="类型" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="typeTagMap[row.type] || 'info'">{{ typeLabels[row.type] || row.type }}</el-tag>
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
            <div v-else-if="selectedRackId && !devicesLoading" class="empty-hint">
              当前机柜下无设备
            </div>
          </div>

          <!-- Command Input -->
          <div class="script-section">
            <div class="section-header">
              <span class="section-title">执行命令</span>
            </div>
            <div class="preset-bar">
              <span class="preset-label">快捷指令:</span>
              <el-button
                v-for="p in commandPresets"
                :key="p.label"
                size="small"
                plain
                @click="applyPreset(p)"
              >{{ p.label }}<span class="preset-os-tag">{{ presetOsTag(p) }}</span></el-button>
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
              style="font-family: 'Cascadia Code', 'Fira Code', Menlo, monospace;"
            />
            <div class="command-footer">
              <div class="timeout-control">
                <span>超时:</span>
                <el-input-number v-model="timeout" :min="5" :max="300" size="small" style="width: 120px;" />
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
                  <code v-if="row.exit_code !== null" :class="row.exit_code === 0 ? 'code-ok' : 'code-err'">{{ row.exit_code }}</code>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column :label="lastResultKind === 'power' ? '执行消息' : '输出预览'" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">{{ row.stdout || row.error || row.stderr || '-' }}</template>
              </el-table-column>
            </el-table>
          </div>
      </el-tab-pane>

      <!-- ====== Tab: Scheduled Tasks ====== -->
      <el-tab-pane label="计划任务" name="scheduled">
          <div class="scheduled-toolbar">
            <el-button type="primary" @click="openCreateDialog">
              <el-icon><Plus /></el-icon> 新建任务
            </el-button>
            <el-button @click="loadScheduledTasks">刷新</el-button>
          </div>

          <el-table :data="scheduledTasks" size="small" stripe v-loading="tasksLoading">
            <el-table-column prop="name" label="任务名称" min-width="120" show-overflow-tooltip />
            <el-table-column prop="command" label="命令" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <code style="font-size: var(--dcn-text-sm);">{{ row.command }}</code>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="80" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.schedule_type === 'once' ? 'info' : 'warning'">
                  {{ row.schedule_type === 'once' ? '一次' : '周期' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="计划" width="170">
              <template #default="{ row }">
                <span v-if="row.schedule_type === 'once'">{{ formatTime(row.scheduled_at) }}</span>
                <span v-else style="font-size: var(--dcn-text-sm);">{{ humanizeCron(row.cron_expression) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="下次执行" width="170">
              <template #default="{ row }">{{ formatTime(row.next_run_at) || '-' }}</template>
            </el-table-column>
            <el-table-column label="上次执行" width="170">
              <template #default="{ row }">{{ formatTime(row.last_run_at) || '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="80" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="taskStatusType(row.status)">{{ taskStatusLabels[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="220" align="center">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'completed'" size="small" type="primary" plain @click="openEditDialog(row)">编辑</el-button>
                <el-button v-if="row.status === 'active' || row.status === 'pending'" size="small" type="warning" plain @click="pauseTask(row)">暂停</el-button>
                <el-button v-if="row.status === 'paused'" size="small" type="success" plain @click="resumeTask(row)">启用</el-button>
                <el-button size="small" type="danger" plain @click="deleteTask(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <!-- Create/Edit Dialog -->
          <el-dialog v-model="showCreateDialog" :title="editingTaskId ? '编辑计划任务' : '新建计划任务'" width="640px" :close-on-click-modal="false">
            <el-form :model="taskForm" label-width="100px" label-position="top">
              <el-form-item label="任务名称" required>
                <el-input v-model="taskForm.name" placeholder="如: 每日磁盘检查" />
              </el-form-item>
              <el-form-item label="执行命令" required>
                <el-input v-model="taskForm.command" type="textarea" :rows="3" placeholder="输入要执行的命令" style="font-family: monospace;" />
              </el-form-item>
              <el-form-item label="目标设备" required>
                <div class="device-select-trigger" @click="showDevicePicker = true">
                  <div class="device-select-tags" v-if="taskForm.device_ids.length > 0">
                    <el-tag
                      v-for="id in taskForm.device_ids"
                      :key="id"
                      closable
                      size="small"
                      @close="removeDevice(id)"
                      class="device-tag"
                    >{{ getDeviceName(id) }}</el-tag>
                  </div>
                  <span v-else class="device-select-placeholder">点击选择目标设备</span>
                  <el-button type="primary" size="small" plain class="device-select-btn">选择设备</el-button>
                </div>
              </el-form-item>
              <div class="form-row">
                <el-form-item label="执行类型" required class="form-row-item">
                  <el-radio-group v-model="taskForm.schedule_type">
                    <el-radio label="once">定时执行</el-radio>
                    <el-radio label="recurring">周期执行</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="超时(秒)" class="form-row-item">
                  <el-input-number v-model="taskForm.timeout" :min="5" :max="300" style="width: 100%;" />
                </el-form-item>
              </div>
              <el-form-item v-if="taskForm.schedule_type === 'once'" label="执行时间" required>
                <el-date-picker
                  v-model="taskForm.scheduled_at"
                  type="datetime"
                  placeholder="选择执行时间"
                  style="width: 100%;"
                  :disabled-date="d => d < new Date(new Date().setHours(0,0,0,0))"
                />
              </el-form-item>
              <el-form-item v-if="taskForm.schedule_type === 'recurring'" label="执行周期" required>
                <div class="schedule-builder">
                  <div class="schedule-row">
                    <span class="schedule-label">频率</span>
                    <el-select v-model="recurringMode" style="width: 160px;">
                      <el-option label="每隔几分钟" value="minutes" />
                      <el-option label="每隔几小时" value="hours" />
                      <el-option label="每天" value="daily" />
                      <el-option label="每周" value="weekly" />
                      <el-option label="每月" value="monthly" />
                    </el-select>
                  </div>
                  <!-- every N minutes -->
                  <div v-if="recurringMode === 'minutes'" class="schedule-row">
                    <span class="schedule-label">间隔</span>
                    <el-input-number v-model="recurringInterval" :min="1" :max="59" style="width: 120px;" />
                    <span class="schedule-unit">分钟</span>
                  </div>
                  <!-- every N hours -->
                  <div v-if="recurringMode === 'hours'" class="schedule-row">
                    <span class="schedule-label">间隔</span>
                    <el-input-number v-model="recurringInterval" :min="1" :max="23" style="width: 120px;" />
                    <span class="schedule-unit">小时</span>
                    <span class="schedule-label" style="margin-left: 16px;">在第</span>
                    <el-input-number v-model="recurringMinute" :min="0" :max="59" style="width: 100px;" />
                    <span class="schedule-unit">分执行</span>
                  </div>
                  <!-- daily -->
                  <div v-if="recurringMode === 'daily'" class="schedule-row">
                    <span class="schedule-label">每天</span>
                    <el-time-picker v-model="recurringTime" format="HH:mm" placeholder="选择时间" style="width: 140px;" />
                    <span class="schedule-unit">执行</span>
                  </div>
                  <!-- weekly -->
                  <div v-if="recurringMode === 'weekly'" class="schedule-row">
                    <span class="schedule-label">每</span>
                    <el-select v-model="recurringWeekday" style="width: 100px;">
                      <el-option v-for="(w, i) in weekdays" :key="i" :label="w" :value="i" />
                    </el-select>
                    <el-time-picker v-model="recurringTime" format="HH:mm" placeholder="选择时间" style="width: 140px;" />
                    <span class="schedule-unit">执行</span>
                  </div>
                  <!-- monthly -->
                  <div v-if="recurringMode === 'monthly'" class="schedule-row">
                    <span class="schedule-label">每月</span>
                    <el-input-number v-model="recurringDay" :min="1" :max="31" style="width: 120px;" />
                    <span class="schedule-unit">日</span>
                    <el-time-picker v-model="recurringTime" format="HH:mm" placeholder="选择时间" style="width: 140px;" />
                    <span class="schedule-unit">执行</span>
                  </div>
                  <div class="schedule-preview">
                    <el-icon><Timer /></el-icon>
                    <span>{{ schedulePreview }}</span>
                  </div>
                </div>
              </el-form-item>
            </el-form>
            <template #footer>
              <el-button @click="showCreateDialog = false">取消</el-button>
              <el-button type="primary" :loading="creating" @click="saveTask">{{ editingTaskId ? '保存' : '创建' }}</el-button>
            </template>
          </el-dialog>

          <!-- Device Picker Dialog -->
          <el-dialog v-model="showDevicePicker" title="选择目标设备" width="780px" append-to-body :close-on-click-modal="false">
            <div class="device-picker">
              <div class="picker-column">
                <div class="picker-column-header">机房</div>
                <div class="picker-column-body">
                  <div
                    v-for="r in rooms"
                    :key="r.id"
                    class="picker-item"
                    :class="{ active: pickerRoomId === r.id }"
                    @click="onPickerRoomClick(r.id)"
                  >{{ r.name }}</div>
                  <div v-if="rooms.length === 0" class="picker-empty">暂无机房</div>
                </div>
              </div>
              <div class="picker-column">
                <div class="picker-column-header">机柜</div>
                <div class="picker-column-body">
                  <div
                    v-for="rk in pickerRacks"
                    :key="rk.id"
                    class="picker-item"
                    :class="{ active: pickerRackId === rk.id }"
                    @click="onPickerRackClick(rk.id)"
                  >
                    <span>{{ rk.name }}</span>
                    <el-tag size="small" type="info" style="margin-left: auto;">{{ rk.name }}</el-tag>
                  </div>
                  <div v-if="pickerRoomId && pickerRacks.length === 0" class="picker-empty">暂无机柜</div>
                  <div v-if="!pickerRoomId" class="picker-empty">请先选择机房</div>
                </div>
              </div>
              <div class="picker-column picker-column-devices">
                <div class="picker-column-header">
                  <span>设备</span>
                  <el-checkbox
                    v-model="pickerAllChecked"
                    :indeterminate="pickerIndeterminate"
                    @change="onPickerCheckAll"
                    size="small"
                  >全选</el-checkbox>
                </div>
                <div class="picker-column-body" v-loading="pickerDevicesLoading">
                  <el-checkbox-group v-model="pickerCheckedIds">
                    <div
                      v-for="d in pickerDevices"
                      :key="d.id"
                      class="picker-device-item"
                    >
                      <el-checkbox :value="d.id" :disabled="!isScriptSupported(d)">
                        <div class="picker-device-info">
                          <span class="picker-device-name">{{ d.name }}</span>
                          <span class="picker-device-ip">{{ d.ip_address || '-' }}</span>
                          <el-tag size="small" :type="typeTagMap[d.type] || 'info'" style="margin-left: 4px;">{{ typeLabels[d.type] || d.type }}</el-tag>
                        </div>
                      </el-checkbox>
                    </div>
                  </el-checkbox-group>
                  <div v-if="pickerRackId && pickerDevices.length === 0 && !pickerDevicesLoading" class="picker-empty">该机柜下无设备</div>
                  <div v-if="!pickerRackId" class="picker-empty">请先选择机柜</div>
                </div>
              </div>
            </div>
            <template #footer>
              <div class="picker-footer">
                <span class="picker-selected-count">已选 {{ pickerCheckedIds.length }} 台设备</span>
                <div>
                  <el-button @click="showDevicePicker = false">取消</el-button>
                  <el-button type="primary" @click="confirmDevicePicker">确定</el-button>
                </div>
              </div>
            </template>
          </el-dialog>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, watch, onMounted } from 'vue'
import { Promotion, Plus, Timer, Refresh, SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { scriptAPI, scheduledTaskAPI, roomAPI, rackAPI, deviceAPI } from '@/api'
import type { Room, Rack, Device, ScheduledTask } from '@/types'

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
  server: '服务器', switch: '交换机', router: '路由器', firewall: '防火墙', host: '主机',
}
const typeTagMap: Record<string, string> = {
  server: '', switch: 'warning', router: 'success', firewall: 'danger', host: 'info',
}
const statusLabels: Record<string, string> = {
  online: '在线', offline: '离线', maintenance: '维护',
}

// 命令模板：点击后填入命令输入框，根据选中设备 OS 自动选择命令
const commandPresets = [
  { label: '主机名', linux: 'hostname', windows: 'hostname' },
  { label: '磁盘', linux: 'df -h', windows: 'Get-Volume | Format-Table -AutoSize' },
  { label: '内存', linux: 'free -h', windows: 'Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory' },
  { label: '运行时间', linux: 'uptime', windows: '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime' },
]

// 电源操作：点击直接调用批量电源接口（SSH→Win32 RPC→RDP 三层回退，支持无 SSH 的 Windows 设备）
const powerPresets = [
  { label: '关机', action: 'shutdown' as const, type: 'danger' as const, icon: SwitchButton },
  { label: '重启', action: 'reboot' as const, type: 'warning' as const, icon: Refresh },
]


function isWindows(device: Device): boolean {
  return (device.os_system || '').toLowerCase().includes('windows')
}

function selectedMajorityOS(): 'linux' | 'windows' {
  const win = selectedDevices.value.filter(d => isWindows(d)).length
  const nix = selectedDevices.value.filter(d => !isWindows(d)).length
  return win > nix ? 'windows' : 'linux'
}

function presetOsTag(preset: typeof commandPresets[0]): string {
  if (selectedDevices.value.length === 0) return ''
  const os = selectedMajorityOS()
  return os === 'windows' ? ' (Win)' : ' (Linux)'
}

function applyPreset(preset: typeof commandPresets[0]) {
  const os = selectedDevices.value.length > 0 ? selectedMajorityOS() : 'linux'
  command.value = os === 'windows' ? preset.windows : preset.linux
}

function osLabel(device: Device): string {
  const os = (device.os_system || '').toLowerCase()
  if (os.includes('windows')) return 'Windows'
  const networkTypes = ['switch', 'router', 'firewall']
  if (networkTypes.includes(device.type)) return 'Network'
  return 'Linux'
}

function osTagType(device: Device): string {
  const os = (device.os_system || '').toLowerCase()
  if (os.includes('windows')) return 'warning'
  const networkTypes = ['switch', 'router', 'firewall']
  if (networkTypes.includes(device.type)) return 'primary'
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

const succeeded = computed(() => results.value.filter(r => r.success).length)
const failed = computed(() => results.value.filter(r => !r.success).length)

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

function isScriptSupported(device: Device): boolean {
  // All devices support SSH execution (Windows via PowerShell)
  return true
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
    await ElMessageBox.confirm(
      `将在 ${selectedDevices.value.length} 台设备上执行命令:\n${cmdPreview}`,
      '确认执行',
      { confirmButtonText: '执行', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  const targetIds = selectedDevices.value.map(d => d.id)
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
  const devicesList = selectedDevices.value.map(d => `  • ${d.name} (${d.ip_address || '-'})`).join('\n')
  try {
    await ElMessageBox.confirm(
      `将对以下 ${selectedDevices.value.length} 台设备执行【${actionLabel}】操作:\n${devicesList}\n\n此操作不可撤销，确认继续？`,
      `批量${actionLabel}确认`,
      { confirmButtonText: `确认${actionLabel}`, cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  const targetIds = selectedDevices.value.map(d => d.id)
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
      error: r.success ? '' : (r.message || ''),
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

// ====== Scheduled tasks state ======
const scheduledTasks = ref<ScheduledTask[]>([])
const tasksLoading = ref(false)
const showCreateDialog = ref(false)
const creating = ref(false)
const editingTaskId = ref<number | null>(null)

// Device picker state
const showDevicePicker = ref(false)
const pickerRoomId = ref<number | null>(null)
const pickerRackId = ref<number | null>(null)
const pickerRacks = ref<Rack[]>([])
const pickerDevices = ref<Device[]>([])
const pickerDevicesLoading = ref(false)
const pickerCheckedIds = ref<number[]>([])

const allPickerDeviceIds = computed(() => pickerDevices.value.filter(d => isScriptSupported(d)).map(d => d.id))

const pickerAllChecked = computed({
  get: () => allPickerDeviceIds.value.length > 0 && allPickerDeviceIds.value.every(id => pickerCheckedIds.value.includes(id)),
  set: () => {},
})

const pickerIndeterminate = computed(() => {
  const checked = pickerCheckedIds.value.filter(id => allPickerDeviceIds.value.includes(id)).length
  return checked > 0 && checked < allPickerDeviceIds.value.length
})

function onPickerCheckAll(val: boolean) {
  if (val) {
    const existing = new Set(pickerCheckedIds.value)
    allPickerDeviceIds.value.forEach(id => existing.add(id))
    pickerCheckedIds.value = [...existing]
  } else {
    pickerCheckedIds.value = pickerCheckedIds.value.filter(id => !allPickerDeviceIds.value.includes(id))
  }
}

async function onPickerRoomClick(roomId: number) {
  pickerRoomId.value = roomId
  pickerRackId.value = null
  pickerDevices.value = []
  pickerRacks.value = []
  try {
    const res = await rackAPI.list(roomId)
    pickerRacks.value = res.data
  } catch { /* ignore */ }
}

async function onPickerRackClick(rackId: number) {
  pickerRackId.value = rackId
  pickerDevices.value = []
  pickerDevicesLoading.value = true
  try {
    const res = await deviceAPI.list(rackId)
    pickerDevices.value = res.data
  } catch { /* ignore */ } finally {
    pickerDevicesLoading.value = false
  }
}

function confirmDevicePicker() {
  taskForm.device_ids = [...pickerCheckedIds.value]
  showDevicePicker.value = false
}

function removeDevice(id: number) {
  taskForm.device_ids = taskForm.device_ids.filter(d => d !== id)
}

function getDeviceName(id: number): string {
  // Search across all loaded device sources
  const d = pickerDevices.value.find(d => d.id === id)
  if (d) return `${d.name} (${d.ip_address || '-'})`
  return `设备 #${id}`
}

const taskForm = reactive({
  name: '',
  command: '',
  device_ids: [] as number[],
  schedule_type: 'once' as 'once' | 'recurring',
  scheduled_at: null as Date | null,
  cron_expression: '',
  timeout: 30,
})

// ====== Recurring schedule builder ======
const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const recurringMode = ref('daily')
const recurringInterval = ref(30)
const recurringMinute = ref(0)
const recurringTime = ref(new Date(2024, 0, 1, 8, 0))
const recurringWeekday = ref(1)
const recurringDay = ref(1)

function buildCronExpression(): string {
  const t = recurringTime.value
  const hh = t ? t.getHours() : 0
  const mm = t ? t.getMinutes() : 0
  switch (recurringMode.value) {
    case 'minutes': return `*/${recurringInterval.value} * * * *`
    case 'hours': return `${recurringMinute.value} */${recurringInterval.value} * * *`
    case 'daily': return `${mm} ${hh} * * *`
    case 'weekly': return `${mm} ${hh} * * ${recurringWeekday.value}`
    case 'monthly': return `${mm} ${hh} ${recurringDay.value} * *`
    default: return '* * * * *'
  }
}

const schedulePreview = computed(() => {
  const t = recurringTime.value
  const hh = t ? String(t.getHours()).padStart(2, '0') : '08'
  const mm = t ? String(t.getMinutes()).padStart(2, '0') : '00'
  switch (recurringMode.value) {
    case 'minutes': return `每隔 ${recurringInterval.value} 分钟执行一次`
    case 'hours': return `每隔 ${recurringInterval.value} 小时，在第 ${recurringMinute.value} 分钟执行`
    case 'daily': return `每天 ${hh}:${mm} 执行`
    case 'weekly': return `每${weekdays[recurringWeekday.value]} ${hh}:${mm} 执行`
    case 'monthly': return `每月 ${recurringDay.value} 日 ${hh}:${mm} 执行`
    default: return ''
  }
})

function humanizeCron(cron: string | null): string {
  if (!cron) return '-'
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return cron
  const [min, hour, day, month, dow] = parts
  if (min.startsWith('*/') && hour === '*') return `每隔 ${min.slice(2)} 分钟`
  if (hour.startsWith('*/') && day === '*' && dow === '*') return `每隔 ${hour.slice(2)} 小时`
  const hh = String(parseInt(hour)).padStart(2, '0')
  const mm = String(parseInt(min)).padStart(2, '0')
  if (day !== '*' && month === '*' && dow === '*') return `每月 ${day} 日 ${hh}:${mm}`
  if (dow !== '*' && day === '*') return `每${weekdays[parseInt(dow)] || ''} ${hh}:${mm}`
  return `每天 ${hh}:${mm}`
}

const taskStatusLabels: Record<string, string> = {
  pending: '等待中', active: '运行中', paused: '已暂停', completed: '已完成', disabled: '已禁用',
}

function taskStatusType(status: string): string {
  const map: Record<string, string> = { pending: 'info', active: 'success', paused: 'warning', completed: '', disabled: 'danger' }
  return map[status] || 'info'
}

function formatTime(val: string | null): string {
  if (!val) return ''
  return new Date(val).toLocaleString('zh-CN')
}

async function loadScheduledTasks() {
  tasksLoading.value = true
  try {
    const res = await scheduledTaskAPI.list()
    scheduledTasks.value = res.data
  } catch {
    ElMessage.error('加载计划任务失败')
  } finally {
    tasksLoading.value = false
  }
}

function openCreateDialog() {
  editingTaskId.value = null
  taskForm.name = ''
  taskForm.command = ''
  taskForm.device_ids = []
  taskForm.schedule_type = 'once'
  taskForm.scheduled_at = null
  taskForm.timeout = 30
  recurringMode.value = 'daily'
  recurringInterval.value = 30
  recurringMinute.value = 0
  recurringTime.value = new Date(2024, 0, 1, 8, 0)
  recurringWeekday.value = 1
  recurringDay.value = 1
  showCreateDialog.value = true
}

function openEditDialog(task: ScheduledTask) {
  editingTaskId.value = task.id
  taskForm.name = task.name
  taskForm.command = task.command
  taskForm.device_ids = [...task.device_ids]
  taskForm.schedule_type = task.schedule_type
  taskForm.scheduled_at = task.scheduled_at ? new Date(task.scheduled_at) : null
  taskForm.timeout = task.timeout

  // Parse existing cron into builder state
  if (task.cron_expression) {
    parseCronToBuilder(task.cron_expression)
  }

  showCreateDialog.value = true
}

function parseCronToBuilder(cron: string) {
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return
  const [min, hour, day, month, dow] = parts

  if (min.startsWith('*/') && hour === '*') {
    recurringMode.value = 'minutes'
    recurringInterval.value = parseInt(min.slice(2)) || 30
  } else if (hour.startsWith('*/') && day === '*' && dow === '*') {
    recurringMode.value = 'hours'
    recurringInterval.value = parseInt(hour.slice(2)) || 1
    recurringMinute.value = parseInt(min) || 0
  } else if (day !== '*' && month === '*' && dow === '*') {
    recurringMode.value = 'monthly'
    recurringDay.value = parseInt(day) || 1
    recurringTime.value = new Date(2024, 0, 1, parseInt(hour) || 0, parseInt(min) || 0)
  } else if (dow !== '*' && day === '*') {
    recurringMode.value = 'weekly'
    recurringWeekday.value = parseInt(dow) || 0
    recurringTime.value = new Date(2024, 0, 1, parseInt(hour) || 0, parseInt(min) || 0)
  } else {
    recurringMode.value = 'daily'
    recurringTime.value = new Date(2024, 0, 1, parseInt(hour) || 0, parseInt(min) || 0)
  }
}

// Pre-fill picker state when opening
watch(showDevicePicker, (val) => {
  if (val) {
    pickerCheckedIds.value = [...taskForm.device_ids]
    pickerRoomId.value = null
    pickerRackId.value = null
    pickerRacks.value = []
    pickerDevices.value = []
  }
})

async function saveTask() {
  if (!taskForm.name.trim()) { ElMessage.warning('请输入任务名称'); return }
  if (!taskForm.command.trim()) { ElMessage.warning('请输入命令'); return }
  if (taskForm.device_ids.length === 0) { ElMessage.warning('请选择设备'); return }

  creating.value = true
  try {
    const payload: Record<string, any> = {
      name: taskForm.name,
      command: taskForm.command,
      device_ids: taskForm.device_ids,
      schedule_type: taskForm.schedule_type,
      timeout: taskForm.timeout,
    }
    if (taskForm.schedule_type === 'once') {
      if (!taskForm.scheduled_at) { ElMessage.warning('请选择执行时间'); creating.value = false; return }
      const d = taskForm.scheduled_at
      const pad = (n: number) => String(n).padStart(2, '0')
      payload.scheduled_at = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    } else {
      payload.cron_expression = buildCronExpression()
    }

    if (editingTaskId.value) {
      await scheduledTaskAPI.update(editingTaskId.value, payload)
      ElMessage.success('计划任务已更新')
    } else {
      await scheduledTaskAPI.create(payload)
      ElMessage.success('计划任务已创建')
    }
    showCreateDialog.value = false
    loadScheduledTasks()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || (editingTaskId.value ? '保存失败' : '创建失败'))
  } finally {
    creating.value = false
  }
}

async function pauseTask(task: ScheduledTask) {
  try {
    await ElMessageBox.confirm(`暂停计划任务「${task.name}」?`, '确认暂停', { type: 'warning' })
    await scheduledTaskAPI.update(task.id, { status: 'paused' })
    ElMessage.success('已暂停')
    loadScheduledTasks()
  } catch { /* cancelled */ }
}

async function resumeTask(task: ScheduledTask) {
  try {
    await scheduledTaskAPI.update(task.id, { status: 'active' })
    ElMessage.success('已启用')
    loadScheduledTasks()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function deleteTask(task: ScheduledTask) {
  try {
    await ElMessageBox.confirm(`删除计划任务「${task.name}」? 此操作不可恢复。`, '确认删除', { type: 'warning' })
    await scheduledTaskAPI.delete(task.id)
    ElMessage.success('已删除')
    loadScheduledTasks()
  } catch { /* cancelled */ }
}

watch(activeTab, (tab) => {
  router.replace({ query: { ...route.query, tab } })
  if (tab === 'scheduled') loadScheduledTasks()
})

onMounted(() => {
  loadRooms()
  if (activeTab.value === 'scheduled') loadScheduledTasks()
})
</script>

<style scoped>
.script-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
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

.status-online { background: var(--dcn-dot-online); }
.status-offline { background: var(--dcn-text-secondary); }
.status-maintenance { background: var(--dcn-dot-maintenance); }

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

.scheduled-toolbar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}

/* ====== Form improvements ====== */
.form-row {
  display: flex;
  gap: var(--dcn-space-4);
}

.form-row-item {
  flex: 1;
}

.field-hint {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  margin-top: var(--dcn-space-1);
  line-height: 1.4;
}

.schedule-radio-group {
  width: 100%;
}

.schedule-radio-group .el-radio-button {
  flex: 1;
}

/* ====== Device select trigger ====== */
.device-select-trigger {
  width: 100%;
  min-height: 40px;
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-sm);
  padding: 4px var(--dcn-space-2);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  transition: border-color var(--dcn-transition-normal);
  background: var(--dcn-bg-card);
}

.device-select-trigger:hover {
  border-color: var(--dcn-primary);
}

.device-select-tags {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: var(--dcn-space-1);
}

.device-tag {
  max-width: 200px;
}

.device-tag :deep(.el-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.device-select-placeholder {
  flex: 1;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-md);
}

.device-select-btn {
  flex-shrink: 0;
}

/* ====== Schedule builder ====== */
.schedule-builder {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
}

.schedule-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}

.schedule-label {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
  flex-shrink: 0;
}

.schedule-unit {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
}

.schedule-preview {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: var(--dcn-space-2) var(--dcn-space-3);
  background: var(--dcn-success-light);
  border-radius: var(--dcn-radius-sm);
  font-size: var(--dcn-text-base);
  color: var(--dcn-success);
  margin-top: var(--dcn-space-1);
}

/* ====== Device Picker Dialog ====== */
.device-picker {
  display: flex;
  gap: 0;
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
  height: 420px;
}

.picker-column {
  flex: 1;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--dcn-border-strong);
}

.picker-column:last-child {
  border-right: none;
  flex: 1.5;
}

.picker-column-header {
  padding: 10px 14px;
  background: var(--dcn-bg-section);
  border-bottom: 1px solid var(--dcn-border-strong);
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: var(--dcn-text-primary);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}

.picker-column-body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.picker-item {
  padding: var(--dcn-space-2) 14px;
  cursor: pointer;
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
  transition: all var(--dcn-transition-fast);
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}

.picker-item:hover {
  background: var(--dcn-bg-section);
  color: var(--dcn-primary);
}

.picker-item.active {
  background: var(--dcn-primary-bg);
  color: var(--dcn-primary);
  font-weight: 500;
}

.picker-empty {
  text-align: center;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-base);
  padding: var(--dcn-space-10) var(--dcn-space-5);
}

.picker-device-item {
  padding: 6px 14px;
}

.picker-device-item:hover {
  background: var(--dcn-bg-elevated);
}

.picker-device-info {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
  flex-wrap: nowrap;
}

.picker-device-name {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-primary);
  min-width: 80px;
}

.picker-device-ip {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

.picker-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.picker-selected-count {
  font-size: var(--dcn-text-base);
  color: var(--dcn-primary);
  font-weight: 500;
}
</style>
