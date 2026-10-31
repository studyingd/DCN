<template>
  <div class="inline-panel alert-center">
    <div class="inline-panel-header alert-header">
      <div>
        <h2 class="inline-panel-title">告警中心</h2>
      </div>
      <el-button type="primary" @click="openCreate"
        ><el-icon><Plus /></el-icon>新建告警规则</el-button
      >
    </div>
    <div class="alert-body">
      <div class="stats-grid">
        <div class="stat-card">
          <span>启用规则</span><strong>{{ overview.enabled_rules }}</strong
          ><el-icon><Bell /></el-icon>
        </div>
        <div class="stat-card" :class="{ danger: overview.open_events > 0 }">
          <span>当前未恢复</span><strong>{{ overview.open_events }}</strong
          ><el-icon><Warning /></el-icon>
        </div>
        <div class="stat-card" :class="{ danger: overview.critical_events > 0 }">
          <span>严重告警</span><strong>{{ overview.critical_events }}</strong
          ><el-icon><CircleCloseFilled /></el-icon>
        </div>
        <div class="stat-card">
          <span>通知失败（24h）</span><strong>{{ overview.failed_notifications }}</strong
          ><el-icon><Promotion /></el-icon>
        </div>
      </div>
      <div class="alert-tabs">
        <button :class="{ active: tab === 'rules' }" @click="tab = 'rules'">告警规则</button
        ><button :class="{ active: tab === 'events' }" @click="tab = 'events'">
          告警事件 <el-badge v-if="overview.open_events" :value="overview.open_events" />
        </button>
      </div>
      <section v-if="tab === 'rules'" class="section-card">
        <div class="section-heading">
          <div>
            <strong>告警规则</strong
            ><small
              >覆盖性能、主机、容器和业务状态；自动处置进度与恢复分别推送 alert.remediation / alert.resolved</small
            >
          </div>
          <el-button :loading="loading" @click="loadAll"
            ><el-icon><Refresh /></el-icon>刷新</el-button
          >
        </div>
        <el-table v-loading="loading" :data="rules" row-key="id" class="alert-table">
          <el-table-column label="规则名称" min-width="210">
            <template #default="{ row }"
              ><div class="rule-name-cell">
                <strong>{{ row.name }}</strong
                ><small>规则 #{{ row.id }} · {{ row.enabled ? '实时评估中' : '已暂停评估' }}</small>
              </div></template
            >
          </el-table-column>
          <el-table-column label="告警条件" min-width="190">
            <template #default="{ row }"
              ><div class="condition-cell">
                <span class="condition-dot" :class="severityType(row.severity)"></span
                ><strong>{{ conditionLabel(row) }}</strong>
              </div></template
            >
          </el-table-column>
          <el-table-column
            label="级别"
            width="96"
            align="center"
            header-align="center"
            class-name="severity-column"
            header-class-name="severity-column-header"
            ><template #default="{ row }"
              ><span class="severity-cell"
                ><el-tag :type="severityType(row.severity)" effect="plain" round>{{
                  severityLabel(row.severity)
                }}</el-tag></span
              ></template
            ></el-table-column
          >
          <el-table-column label="监控范围" min-width="145"
            ><template #default="{ row }"
              ><span class="scope-cell"
                ><el-icon><Monitor /></el-icon><span>{{ scopeLabel(row) }}</span></span
              ></template
            ></el-table-column
          >
          <el-table-column label="触发策略" width="142"
            ><template #default="{ row }"
              ><div class="timing-cell">
                <span
                  ><b>{{ row.sustain_seconds }}s</b> 持续</span
                ><span
                  ><b>{{ row.cooldown_seconds }}s</b> 冷却</span
                >
              </div></template
            ></el-table-column
          >
          <el-table-column label="当前告警" width="105" align="center"
            ><template #default="{ row }"
              ><span class="active-count" :class="{ danger: row.active_event_count > 0 }"
                ><el-icon v-if="row.active_event_count > 0"><Warning /></el-icon
                >{{ row.active_event_count > 0 ? row.active_event_count : '无' }}</span
              ></template
            ></el-table-column
          >
          <el-table-column label="状态" width="96"
            ><template #default="{ row }"
              ><div class="status-cell">
                <el-switch v-model="row.enabled" @change="toggleRule(row)" /><small>{{
                  row.enabled ? '已启用' : '已停用'
                }}</small>
              </div></template
            ></el-table-column
          >
          <el-table-column label="操作" width="135" fixed="right"
            ><template #default="{ row }"
              ><el-button link type="primary" @click="openEdit(row)">编辑</el-button
              ><el-button link type="danger" @click="removeRule(row)">删除</el-button></template
            ></el-table-column
          >
        </el-table>
        <el-empty v-if="!loading && rules.length === 0" description="还没有告警规则" />
      </section>
      <!-- 事件子表用 v-show 保活:筛选/分页/归因弹窗状态跨页签保留(与拆分前一致) -->
      <EventTable v-show="tab === 'events'" ref="eventTableRef" />
    </div>

    <RuleFormDialog ref="ruleFormRef" @saved="loadAll" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { usePolling } from '@/composables/usePolling'
import { Bell, CircleCloseFilled, Monitor, Plus, Promotion, Refresh, Warning } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { alertAPI } from '@/api'
import EventTable from '@/components/panel/alerts/EventTable.vue'
import RuleFormDialog from '@/components/panel/alerts/RuleFormDialog.vue'
import type { AlertRule } from '@/types'
import { loadAlertLabels, metricLabel, numericMetrics, severityLabel, severityType } from './alerts/alertShared'

const tab = ref<'rules' | 'events'>('rules')
const loading = ref(false)
const rules = ref<AlertRule[]>([])
const ruleFormRef = ref<InstanceType<typeof RuleFormDialog>>()
const eventTableRef = ref<InstanceType<typeof EventTable>>()

const overview = reactive({
  active_rules: 0,
  enabled_rules: 0,
  open_events: 0,
  critical_events: 0,
  events_24h: 0,
  failed_notifications: 0,
})

const conditionLabel = (row: AlertRule) =>
  numericMetrics.includes(row.metric)
    ? `${metricLabel(row.metric)} ${row.operator === 'gte' ? '≥' : '>'} ${row.threshold}%`
    : metricLabel(row.metric)
const scopeLabel = (row: AlertRule) => {
  if (row.metric.startsWith('container_')) {
    const devices = row.target_device_ids?.length || 0
    const containers = row.target_container_ids?.length || 0
    if (!devices && !containers) return '全部容器'
    return containers ? `${devices} 台设备 · ${containers} 个容器` : `${devices} 台设备`
  }
  if (row.metric.startsWith('business_'))
    return row.target_business_ids?.length ? `${row.target_business_ids.length} 个业务` : '全部业务'
  const devices = row.target_device_ids?.length || 0
  if (!devices) return '全部资源'
  const guests = (row.target_device_ids || []).filter((id) => id < 0).length
  return guests ? `${devices - guests} 台设备 · ${guests} 台虚拟机` : `${devices} 台设备`
}

async function loadOverview() {
  const o = await alertAPI.overview()
  Object.assign(overview, o.data)
}
async function loadRules() {
  rules.value = (await alertAPI.rules()).data
}
let loadInFlight: Promise<void> | null = null
async function loadAll() {
  if (loadInFlight) return loadInFlight
  loading.value = true
  loadInFlight = (async () => {
    try {
      await Promise.all([loadOverview(), loadRules()])
      // 规则弹窗开着时同步刷新监控对象候选(原 if (formVisible) loadTargets 语义)
      await ruleFormRef.value?.refreshTargetsIfOpen()
    } catch {
      ElMessage.error('加载告警中心失败')
    } finally {
      loading.value = false
      loadInFlight = null
    }
  })()
  return loadInFlight
}

function openCreate() {
  void ruleFormRef.value?.open()
}
function openEdit(row: AlertRule) {
  void ruleFormRef.value?.open(row)
}
async function toggleRule(row: AlertRule) {
  try {
    const { data } = await alertAPI.updateRule(row.id, { enabled: row.enabled })
    const closed = data?.closed_event_count || 0
    if (closed) ElMessage.success(`规则已停用，同时关闭了 ${closed} 条进行中告警`)
    else if (!row.enabled) ElMessage.success('规则已停用')
    await loadAll()
  } catch {
    row.enabled = !row.enabled
    ElMessage.error('更新规则状态失败')
  }
}
async function removeRule(row: AlertRule) {
  try {
    await ElMessageBox.confirm(
      `确定删除“${row.name}”吗？进行中的告警会一并关闭，历史告警记录会保留。`,
      '删除告警规则',
      { type: 'warning' },
    )
    const { data } = await alertAPI.deleteRule(row.id)
    ElMessage.success(data?.message || '告警规则已删除')
    await loadAll()
  } catch {
    /* cancelled */
  }
}

// 30s 主刷新统一走 usePolling(2026-10-31 收编):隐藏不发请求、恢复可见
// 立即补一轮;各 load 自行 catch 保持轮询链不断(setInterval 时代语义)
const { start: startRefresh, stop: stopRefresh } = usePolling(
  () => {
    void loadOverview().catch(() => {})
    if (tab.value === 'rules') void loadRules().catch(() => {})
    else void eventTableRef.value?.refreshQuiet().catch(() => {})
  },
  { intervalMs: 30_000 },
)
onMounted(() => {
  void loadAlertLabels()
  void loadAll()
  if (tab.value === 'events') void eventTableRef.value?.refreshQuiet()
  startRefresh()
})
watch(tab, (value) => {
  if (value === 'events') void eventTableRef.value?.refreshQuiet()
  else void Promise.all([loadOverview(), loadRules()])
})
onUnmounted(() => {
  stopRefresh()
})
</script>

<style scoped>
.alert-center {
  background: var(--dcn-bg-page);
}
.alert-header {
  align-items: center;
}
.alert-header > div:first-child {
  min-width: 0;
}
.alert-header > .el-button {
  align-self: center;
  min-height: 40px;
  padding-inline: 18px;
}
.alert-header p {
  margin: 7px 0 0;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.alert-body {
  padding: 0 var(--dcn-space-5) var(--dcn-space-5);
  overflow: auto;
}
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  padding: 4px 0 18px;
}
.stat-card {
  position: relative;
  padding: 16px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-card);
}
.stat-card span {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.stat-card strong {
  display: block;
  margin-top: 10px;
  color: var(--dcn-text-primary);
  font-size: 27px;
}
.stat-card .el-icon {
  position: absolute;
  right: 16px;
  top: 17px;
  color: var(--dcn-primary-light);
  font-size: 20px;
}
.stat-card.danger strong,
.stat-card.danger .el-icon {
  color: var(--dcn-danger);
}
.alert-tabs {
  display: flex;
  gap: 6px;
  border-bottom: 1px solid var(--dcn-border);
  margin-bottom: 16px;
}
.alert-tabs button {
  padding: 11px 16px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--dcn-text-secondary);
  cursor: pointer;
  font: inherit;
}
.alert-tabs button.active {
  border-color: var(--dcn-primary);
  color: var(--dcn-primary);
  font-weight: 600;
}
.section-card {
  padding: 18px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
}
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.section-heading > div:first-child {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.section-heading small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
@media (max-width: 900px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 600px) {
  .alert-body {
    padding-inline: 12px;
  }
  .stats-grid {
    grid-template-columns: 1fr;
  }
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
}
.alert-body {
  padding-top: 20px;
}
.alert-table :deep(.el-table__header-wrapper th) {
  height: 44px;
  background: var(--dcn-bg-card);
  color: var(--dcn-text-secondary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.alert-table :deep(.el-table__body tr) {
  transition: background-color 0.16s ease;
}
.alert-table :deep(.el-table__body tr:hover > td) {
  background: color-mix(in srgb, var(--dcn-primary) 5%, var(--dcn-bg-card));
}
.alert-table :deep(.el-table__cell) {
  padding: 12px 0;
}
.rule-name-cell,
.condition-cell,
.timing-cell,
.status-cell {
  display: flex;
  flex-direction: column;
}
.rule-name-cell {
  gap: 4px;
}
.rule-name-cell strong,
.condition-cell strong {
  color: var(--dcn-text-primary);
  font-size: 13px;
  font-weight: 650;
}
.rule-name-cell small,
.condition-cell small,
.timing-cell span,
.status-cell small {
  color: var(--dcn-text-secondary);
  font-size: 11px;
  line-height: 1.45;
  /* 业务的异常明细等消息自带换行,按真实换行渲染(2026-09-18) */
  white-space: pre-line;
}
.condition-cell {
  align-items: center;
}
.condition-cell .condition-dot {
  margin-top: 0;
  transform: translateY(1px);
}
.alert-table .condition-cell {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  line-height: 22px;
}
.alert-table .condition-cell strong {
  display: block;
  line-height: 22px;
}
.alert-table .condition-cell .condition-dot {
  display: block;
  margin: 0;
  transform: translateY(0);
}
.condition-cell {
  flex-direction: row;
  align-items: flex-start;
  gap: 9px;
}
.condition-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--dcn-info);
}
.condition-dot.warning {
  background: var(--dcn-warning);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--dcn-warning) 12%, transparent);
}
.condition-dot.danger {
  background: var(--dcn-danger);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--dcn-danger) 12%, transparent);
}
.scope-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--dcn-text-primary);
  font-size: 12px;
}
.scope-cell .el-icon {
  color: var(--dcn-primary-light);
}
.timing-cell {
  gap: 3px;
}
.timing-cell b {
  color: var(--dcn-text-primary);
  font: 600 12px var(--dcn-font-mono);
}
.active-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 34px;
  color: var(--dcn-text-secondary);
  font: 600 12px var(--dcn-font-mono);
}
.active-count.danger {
  color: var(--dcn-danger);
}
.status-cell {
  align-items: flex-start;
  gap: 3px;
}
.status-cell small {
  font-size: 10px;
}
@media (max-width: 900px) {
  .alert-table :deep(.el-table__body-wrapper) {
    overflow-x: auto;
  }
}
.alert-table :deep(th.severity-column-header .cell),
.alert-table :deep(td.severity-column .cell) {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  line-height: 24px;
}
.severity-cell {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 54px;
  min-height: 24px;
  line-height: 1;
  vertical-align: middle;
}
.severity-cell :deep(.el-tag) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  margin: 0;
  line-height: 22px;
  vertical-align: middle;
}
</style>
