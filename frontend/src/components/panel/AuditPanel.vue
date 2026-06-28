<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">审计中心</h2>
    </div>
    <div class="inline-panel-body">
      <el-tabs :model-value="modelValue" @tab-change="onTabChange">
        <!-- Settings Tab -->
        <el-tab-pane label="功能设置" name="settings">
          <div class="audit-settings">
            <div class="setting-row">
              <div class="setting-info">
                <div class="setting-label">审计日志</div>
                <div class="setting-desc">记录用户 SSH 会话及执行的命令，支持按会话查看和用户筛选</div>
              </div>
              <el-switch v-model="auditSettings.audit_enabled" active-value="true" inactive-value="false" />
            </div>
            <div class="setting-row">
              <div class="setting-info">
                <div class="setting-label">操作录像</div>
                <div class="setting-desc">录制所有 SSH 终端会话，保存为 asciicast 格式，支持事后回放</div>
              </div>
              <el-switch v-model="auditSettings.recording_enabled" active-value="true" inactive-value="false" />
            </div>
            <div class="setting-row">
              <div class="setting-info">
                <div class="setting-label">高危命令拦截</div>
                <div class="setting-desc">检测并阻断危险命令（如 rm -rf /），防止误操作</div>
              </div>
              <el-switch v-model="auditSettings.command_interception_enabled" active-value="true" inactive-value="false" />
            </div>
            <div class="setting-row">
              <div class="setting-info">
                <div class="setting-label">日志保留天数</div>
                <div class="setting-desc">审计日志自动清理周期，超过保留天数的记录将被自动删除</div>
              </div>
              <el-input-number v-model="auditRetentionDays" :min="1" :max="3650" size="small" style="width: 120px;" />
            </div>
            <div class="setting-row">
              <div class="setting-info">
                <div class="setting-label">录像保留天数</div>
                <div class="setting-desc">会话录像自动清理周期，超过保留天数的录像将被自动删除</div>
              </div>
              <el-input-number v-model="recordingRetentionDays" :min="1" :max="3650" size="small" style="width: 120px;" />
            </div>
            <div v-if="auditSettings.command_interception_enabled === 'true'" class="blocked-commands-section">
              <div class="setting-label" style="margin-bottom: 8px;">拦截规则（正则表达式）</div>
              <div v-for="(cmd, idx) in blockedCommandsList" :key="idx" class="blocked-cmd-row">
                <code class="blocked-cmd-code">{{ cmd }}</code>
                <el-button type="danger" link size="small" @click="blockedCommandsList.splice(idx, 1)">删除</el-button>
              </div>
              <div class="add-cmd-row">
                <el-input v-model="newBlockedCmd" placeholder="输入正则表达式，如 rm\s+-rf" size="small" style="flex:1;" />
                <el-button type="primary" size="small" @click="addBlockedCmd">添加</el-button>
              </div>
            </div>
            <el-button type="primary" style="margin-top: 16px;" :loading="savingSettings" @click="saveAuditSettings">保存设置</el-button>
          </div>
        </el-tab-pane>

        <!-- Audit Sessions Tab -->
        <el-tab-pane label="审计日志" name="logs">
          <div class="recordings-layout">
            <div class="recordings-list">
              <div class="audit-filter-bar" style="display: flex; gap: 12px; margin-bottom: 12px; align-items: center;">
                <el-select v-model="sessions.userFilter" placeholder="筛选用户" clearable size="small" style="width: 160px;">
                  <el-option v-for="u in auditUsers" :key="u" :label="u" :value="u" />
                </el-select>
                <el-date-picker
                  v-model="sessions.dateRange"
                  type="daterange"
                  range-separator="至"
                  start-placeholder="开始日期"
                  end-placeholder="结束日期"
                  size="small"
                  style="width: 200px;"
                  value-format="YYYY-MM-DD"
                />
                <el-button size="small" @click="sessions.load({ limit: 50 })">刷新</el-button>
              </div>
              <el-table
                :data="sessions.items"
                stripe
                size="small"
                height="100%"
                v-loading="sessions.loading"
                highlight-current-row
                @current-change="onSessionSelect"
              >
                <el-table-column label="时间" width="150">
                  <template #default="{ row }">{{ formatAuditDate(row.started_at) }}</template>
                </el-table-column>
                <el-table-column prop="username" label="用户" width="80" />
                <el-table-column prop="device_name" label="设备" min-width="90" />
                <el-table-column label="命令" width="65">
                  <template #default="{ row }">
                    {{ row.command_count }}
                    <el-tag v-if="row.blocked_count > 0" size="small" type="danger" style="margin-left: 2px;">{{ row.blocked_count }}</el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <div class="recordings-player">
              <template v-if="showSessionCommands">
                <div class="replay-header">
                  <span class="replay-title">{{ sessionCommandsTitle }}</span>
                  <el-button size="small" text @click="showSessionCommands = false">关闭</el-button>
                </div>
                <div v-if="sessionCommands.length === 0 && !loadingCommands" style="color: var(--dcn-text-secondary); text-align: center; padding: 40px;">本次会话无命令记录</div>
                <div v-else class="cmd-list" v-loading="loadingCommands">
                  <div v-for="cmd in sessionCommands" :key="cmd.id" class="cmd-item">
                    <div class="cmd-item-header">
                      <el-tag v-if="cmd.event_type === 'command_blocked'" type="danger" size="small">拦截</el-tag>
                      <el-tag v-else type="success" size="small">执行</el-tag>
                      <span class="cmd-item-time">{{ formatTimeOnly(cmd.created_at) }}</span>
                    </div>
                    <code class="cmd-item-text">{{ cmd.command }}</code>
                  </div>
                </div>
              </template>
              <div v-else class="replay-placeholder">
                <el-icon :size="48" style="color: var(--dcn-text-placeholder);"><Document /></el-icon>
                <p>点击左侧会话查看命令详情</p>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <!-- Recordings Tab -->
        <el-tab-pane label="会话录像" name="recordings">
          <div class="recordings-layout recordings-layout--full">
            <div class="recordings-list recordings-list--full">
              <div class="audit-filter-bar" style="display: flex; gap: 12px; margin-bottom: 12px; align-items: center;">
                <el-select v-model="recs.userFilter" placeholder="筛选用户" clearable size="small" style="width: 140px;">
                  <el-option v-for="u in auditUsers" :key="u" :label="u" :value="u" />
                </el-select>
                <el-date-picker
                  v-model="recs.dateRange"
                  type="daterange"
                  range-separator="至"
                  start-placeholder="开始日期"
                  end-placeholder="结束日期"
                  size="small"
                  style="width: 200px;"
                  value-format="YYYY-MM-DD"
                />
                <el-button size="small" @click="recs.load()">刷新</el-button>
                <span style="color: var(--dcn-text-secondary); font-size: 13px; margin-left: auto;">点击录像行开始回放</span>
              </div>
              <el-table :data="recs.items" stripe size="small" height="100%" v-loading="recs.loading"
                highlight-current-row @current-change="(row: any) => row && emit('open-replay', row)">
                <el-table-column prop="started_at" label="时间" width="160">
                  <template #default="{ row }">{{ formatAuditDate(row.started_at) }}</template>
                </el-table-column>
                <el-table-column label="用户" width="90">
                  <template #default="{ row }">{{ row.operator || row.username }}</template>
                </el-table-column>
                <el-table-column prop="device_name" label="设备" min-width="100" />
                <el-table-column label="类型" width="70" align="center">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.conn_type === 'rdp' ? 'warning' : ''">{{ row.conn_type === 'rdp' ? 'RDP' : 'SSH' }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="时长" width="70">
                  <template #default="{ row }">{{ row.duration_seconds ? row.duration_seconds + 's' : '-' }}</template>
                </el-table-column>
                <el-table-column prop="file_size" label="文件大小" width="100">
                  <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
                </el-table-column>
                <el-table-column label="操作" width="80" align="center">
                  <template #default="{ row }">
                    <el-button size="small" type="primary" link @click.stop="emit('open-replay', row)">回放</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </el-tab-pane>

        <!-- Script Records Tab -->
        <el-tab-pane label="脚本记录" name="scripts">
          <div class="audit-filter-bar" style="display: flex; gap: 12px; margin-bottom: 12px; align-items: center;">
            <el-select v-model="scripts.userFilter" placeholder="筛选用户" clearable size="small" style="width: 160px;">
              <el-option v-for="u in auditUsers" :key="u" :label="u" :value="u" />
            </el-select>
            <el-date-picker
              v-model="scripts.dateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              size="small"
              style="width: 200px;"
              value-format="YYYY-MM-DD"
            />
            <el-button size="small" @click="scripts.load({ limit: 100 })">刷新</el-button>
          </div>
          <el-table :data="scripts.items" stripe size="small" max-height="420" v-loading="scripts.loading">
            <el-table-column label="时间" width="160">
              <template #default="{ row }">{{ formatAuditDate(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="username" label="用户" width="80" />
            <el-table-column label="命令" min-width="160">
              <template #default="{ row }">
                <code style="font-size: var(--dcn-text-sm); word-break: break-all;">{{ row.command }}</code>
              </template>
            </el-table-column>
            <el-table-column label="设备" min-width="120">
              <template #default="{ row }">
                <span v-for="(d, i) in row.devices" :key="i">
                  {{ d }}<template v-if="i < row.devices.length - 1">、</template>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="100" align="center">
              <template #default="{ row }">
                <el-tag type="success" size="small">{{ row.succeeded }} 成功</el-tag>
                <el-tag v-if="row.failed > 0" type="danger" size="small" style="margin-left: 4px;">{{ row.failed }} 失败</el-tag>
              </template>
            </el-table-column>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div style="padding: 12px 16px;">
                  <el-table :data="row.device_results" size="small" stripe>
                    <el-table-column prop="device_name" label="设备" min-width="120" />
                    <el-table-column prop="ip" label="IP" width="140">
                      <template #default="{ row: r }">{{ r.ip || '-' }}</template>
                    </el-table-column>
                    <el-table-column label="状态" width="80" align="center">
                      <template #default="{ row: r }">
                        <el-tag :type="r.success ? 'success' : 'danger'" size="small">{{ r.success ? '成功' : '失败' }}</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column label="退出码" width="80" align="center">
                      <template #default="{ row: r }">
                        <code v-if="r.exit_code !== null" :style="{ color: r.exit_code === 0 ? 'var(--dcn-success)' : 'var(--dcn-danger)' }">{{ r.exit_code }}</code>
                        <span v-else>-</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="输出" min-width="200">
                      <template #default="{ row: r }">
                        <pre v-if="r.stdout" style="margin:0; font-size:var(--dcn-text-sm); white-space:pre-wrap; word-break:break-all; max-height:120px; overflow-y:auto;">{{ r.stdout }}</pre>
                        <pre v-if="r.stderr" style="margin:0; font-size:var(--dcn-text-sm); color:var(--dcn-danger); white-space:pre-wrap; word-break:break-all;">{{ r.stderr }}</pre>
                        <span v-if="r.error" style="color:var(--dcn-danger); font-size:var(--dcn-text-sm);">{{ r.error }}</span>
                        <span v-if="!r.stdout && !r.stderr && !r.error">-</span>
                      </template>
                    </el-table-column>
                  </el-table>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- Login History Tab -->
        <el-tab-pane label="登录记录" name="logins">
          <div class="audit-filter-bar" style="display: flex; gap: 12px; margin-bottom: 12px; align-items: center;">
            <el-select v-model="logins.userFilter" placeholder="筛选用户" clearable size="small" style="width: 160px;">
              <el-option v-for="u in auditUsers" :key="u" :label="u" :value="u" />
            </el-select>
            <el-date-picker
              v-model="logins.dateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              size="small"
              style="width: 200px;"
              value-format="YYYY-MM-DD"
            />
            <el-button size="small" @click="logins.load({ limit: 100 })">刷新</el-button>
          </div>
          <el-table :data="logins.items" stripe size="small" max-height="420" v-loading="logins.loading">
            <el-table-column prop="username" label="用户" width="120" />
            <el-table-column label="登录时间" min-width="200">
              <template #default="{ row }">{{ formatAuditDate(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="ip" label="来源 IP" width="160">
              <template #default="{ row }">{{ row.ip || '-' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, onMounted } from 'vue'
import { Document } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { settingsAPI, auditAPI } from '@/api'
import { useAuditLoader } from '@/composables/useAuditLoader'

const props = defineProps<{
  modelValue: string
  auditUsers: string[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', tab: string): void
  (e: 'open-replay', row: any): void
}>()

function onTabChange(tab: string) {
  emit('update:modelValue', tab)
}

// ---- Settings ----
const auditSettings = reactive<Record<string, string>>({
  audit_enabled: 'true',
  recording_enabled: 'false',
  command_interception_enabled: 'false',
  audit_retention_days: '90',
  recording_retention_days: '90',
  blocked_commands: '[]',
})
const auditRetentionDays = ref(90)
const recordingRetentionDays = ref(90)
const blockedCommandsList = ref<string[]>([])
const newBlockedCmd = ref('')
const savingSettings = ref(false)

async function loadAuditSettings() {
  try {
    const res = await settingsAPI.get()
    Object.assign(auditSettings, res.data)
    auditRetentionDays.value = parseInt(res.data.audit_retention_days || '90')
    recordingRetentionDays.value = parseInt(res.data.recording_retention_days || '90')
    try { blockedCommandsList.value = JSON.parse(res.data.blocked_commands || '[]') } catch { blockedCommandsList.value = [] }
  } catch { /* ignore */ }
}

function addBlockedCmd() {
  const cmd = newBlockedCmd.value.trim()
  if (!cmd || blockedCommandsList.value.includes(cmd)) return
  try { new RegExp(cmd) } catch { ElMessage.warning('无效的正则表达式'); return }
  blockedCommandsList.value.push(cmd)
  newBlockedCmd.value = ''
}

async function saveAuditSettings() {
  savingSettings.value = true
  try {
    await settingsAPI.update({
      audit_enabled: auditSettings.audit_enabled,
      recording_enabled: auditSettings.recording_enabled,
      command_interception_enabled: auditSettings.command_interception_enabled,
      audit_retention_days: String(auditRetentionDays.value),
      recording_retention_days: String(recordingRetentionDays.value),
      blocked_commands: JSON.stringify(blockedCommandsList.value),
    })
    ElMessage.success('设置已保存')
  } catch { ElMessage.error('保存失败') } finally { savingSettings.value = false }
}

// ---- Data loaders (using composable to eliminate duplication) ----
const sessions = useAuditLoader<any>((params) => auditAPI.listSessions(params))
const recs = useAuditLoader<any>((params) => auditAPI.listRecordings(params))
const logins = useAuditLoader<any>((params) => auditAPI.listLoginHistory(params))
const scripts = useAuditLoader<any>((params) => auditAPI.listScriptRecords(params))

// ---- Session commands ----
const showSessionCommands = ref(false)
const sessionCommandsTitle = ref('')
const sessionCommands = ref<any[]>([])
const loadingCommands = ref(false)

async function openSessionCommands(row: any) {
  sessionCommandsTitle.value = row.display_name
  sessionCommands.value = []
  loadingCommands.value = true
  showSessionCommands.value = true
  try {
    const res = await auditAPI.getSessionCommands(row.session_id)
    sessionCommands.value = res.data
  } catch { /* ignore */ } finally { loadingCommands.value = false }
}

function onSessionSelect(row: any) {
  if (row) openSessionCommands(row)
}

// ---- Watchers ----
watch(() => sessions.userFilter, () => sessions.load({ limit: 50 }))
watch(() => sessions.dateRange, () => sessions.load({ limit: 50 }))
watch(() => recs.userFilter, () => recs.load())
watch(() => recs.dateRange, () => recs.load())
watch(() => logins.userFilter, () => logins.load({ limit: 100 }))
watch(() => logins.dateRange, () => logins.load({ limit: 100 }))
watch(() => scripts.userFilter, () => scripts.load({ limit: 100 }))
watch(() => scripts.dateRange, () => scripts.load({ limit: 100 }))

// ---- Utilities ----
function formatAuditDate(d: string | null): string {
  if (!d) return '-'
  try { return new Date(d).toLocaleString('zh-CN') } catch { return d }
}

function formatTimeOnly(d: string | null): string {
  if (!d) return '-'
  try { return new Date(d).toLocaleTimeString('zh-CN') } catch { return d }
}

function formatFileSize(bytes: number | null): string {
  if (!bytes || bytes === 0) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}

// ---- Initial load ----
onMounted(() => {
  loadAuditSettings()
  sessions.load({ limit: 50 })
  recs.load()
  logins.load({ limit: 100 })
  scripts.load({ limit: 100 })
})

// Expose reload for parent
defineExpose({ loadAuditSettings })
</script>

<style scoped>
/* .inline-panel styles moved to utilities.css for shared use */

/* Audit Center */
.audit-settings {
  padding: var(--dcn-space-2) 0;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--dcn-space-4) 0;
  border-bottom: 1px solid var(--dcn-border);
}

.setting-row:first-child {
  padding-top: 0;
}

.setting-info {
  flex: 1;
}

.setting-label {
  font-size: var(--dcn-text-md);
  font-weight: 500;
  color: var(--dcn-text-primary);
}

.setting-desc {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  margin-top: var(--dcn-space-1);
}

.blocked-commands-section {
  margin-top: var(--dcn-space-4);
  padding: var(--dcn-space-3);
  background: var(--dcn-bg-section);
  border-radius: var(--dcn-radius-md);
}

.blocked-cmd-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 0;
}

.blocked-cmd-code {
  font-size: var(--dcn-text-base);
  background: var(--dcn-bg-card);
  padding: 2px var(--dcn-space-2);
  border-radius: var(--dcn-radius-sm);
  border: 1px solid var(--dcn-border-strong);
}

.add-cmd-row {
  display: flex;
  gap: var(--dcn-space-2);
  margin-top: var(--dcn-space-2);
}

.cmd-list {
  max-height: 500px;
  overflow-y: auto;
}

.cmd-item {
  padding: 10px var(--dcn-space-4);
  border-bottom: 1px solid var(--dcn-border-light);
}

.cmd-item-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: 6px;
}

.cmd-item-time {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

.cmd-item-text {
  display: block;
  font-size: var(--dcn-text-base);
  background: var(--dcn-bg-section);
  padding: 6px 10px;
  border-radius: var(--dcn-radius-sm);
  color: var(--dcn-text-primary);
  word-break: break-all;
  white-space: pre-wrap;
}

/* Recordings layout */
.recordings-layout {
  display: flex;
  gap: var(--dcn-space-5);
  height: 620px;
}

.recordings-layout--full {
  height: 100%;
}

.recordings-list {
  width: 420px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.recordings-list--full {
  width: 100%;
  flex: 1;
}

.recordings-player {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.replay-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--dcn-space-2);
}

.replay-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.replay-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--dcn-text-placeholder);
}

.replay-placeholder p {
  margin-top: var(--dcn-space-3);
  font-size: var(--dcn-text-md);
}

.audit-filter-bar :deep(.el-date-editor--daterange) {
  flex: none !important;
  width: 200px !important;
}
</style>
