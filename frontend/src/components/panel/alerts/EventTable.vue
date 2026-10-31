<template>
  <section class="section-card">
    <div class="section-heading">
      <div><strong>告警事件</strong><small>查看当前异常与历史恢复记录(支持搜索、时间范围与翻页)</small></div>
      <div class="event-filters">
        <el-select v-model="eventStatus" clearable placeholder="状态" style="width: 100px" @change="onFilterChange"
          ><el-option label="未恢复" value="open" /><el-option label="已恢复" value="resolved" /></el-select
        ><el-select v-model="eventSeverity" clearable placeholder="级别" style="width: 100px" @change="onFilterChange"
          ><el-option label="信息" value="info" /><el-option label="警告" value="warning" /><el-option
            label="严重"
            value="critical" /></el-select
        ><el-select v-model="eventMetric" clearable placeholder="指标" style="width: 140px" @change="onFilterChange"
          ><el-option label="CPU 使用率" value="cpu_pct" /><el-option label="内存使用率" value="mem_pct" /><el-option
            label="磁盘使用率"
            value="disk_max_pct" /><el-option label="主机状态" value="host_status" /><el-option
            label="容器状态"
            value="container_status" /><el-option label="业务状态" value="business_status" /></el-select
        ><el-date-picker
          v-model="eventRange"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          :shortcuts="rangeShortcuts"
          style="width: 330px"
          @change="onFilterChange"
        />
        <el-input
          v-model="eventSearch"
          clearable
          placeholder="搜索规则/对象/内容"
          :prefix-icon="Search"
          style="width: 180px"
          @input="onSearchInput"
          @clear="onFilterChange"
        />
        <el-button :loading="eventsLoading" @click="fetchEvents()"
          ><el-icon><Refresh /></el-icon>刷新</el-button
        ><el-button link @click="resetEventFilters">重置</el-button
        ><el-button link type="warning" @click="maintenanceRef?.open()"
          ><el-icon><Mute /></el-icon>维护静默</el-button
        >
      </div>
    </div>
    <el-table v-loading="eventsLoading" :data="events" row-key="id" class="alert-table event-table">
      <el-table-column label="告警事件" min-width="300"
        ><template #default="{ row }"
          ><div class="event-main">
            <span class="event-severity" :class="severityType(row.severity)"
              ><el-icon><Warning /></el-icon>{{ severityLabel(row.severity) }}</span
            ><span class="event-copy"
              ><strong>{{ row.rule_name }}</strong
              ><small :title="row.message">{{ row.message }}</small></span
            >
          </div></template
        ></el-table-column
      >
      <el-table-column label="对象" min-width="205"
        ><template #default="{ row }"
          ><div class="object-cell">
            <strong>{{ row.device_name }}</strong
            ><small>{{ row.device_ip || row.resource_name || row.resource_type || '-' }}</small>
          </div></template
        ></el-table-column
      >
      <el-table-column label="通知状态" width="110"
        ><template #default="{ row }"
          ><el-tag
            :type="
              row.notification_status === 'failed' ? 'danger' : row.notification_status === 'sent' ? 'success' : 'info'
            "
            effect="plain"
            round
            >{{ notifyLabel(row.notification_status) }}</el-tag
          ></template
        ></el-table-column
      >
      <el-table-column label="自动处置 / 归因" min-width="250"
        ><template #default="{ row }"
          ><div v-if="canRemediate(row)" class="heal-cell">
            <div class="heal-tag-row">
              <el-tag :type="remediationTag(row.remediation_state)" effect="plain" round>{{
                remediationLabel(row.remediation_state)
              }}</el-tag
              ><el-button
                v-if="row.status === 'open'"
                link
                type="primary"
                :loading="healBusy === row.id"
                @click="remediate(row)"
                >尝试拉起</el-button
              >
            </div>
            <small v-if="healHint(row)" :title="row.remediation_detail || ''">{{ healHint(row) }}</small>
          </div>
          <div v-else-if="canAnalyze(row)" class="heal-cell">
            <div class="heal-tag-row">
              <el-tag :type="analysisTag(row.analysis_state)" effect="plain" round>{{
                analysisLabel(row.analysis_state)
              }}</el-tag
              ><el-button v-if="row.analysis_state" link type="primary" @click="openAnalysis(row)">查看归因</el-button
              ><el-button v-else link type="primary" :loading="healBusy === row.id" @click="analyze(row)"
                >AI 归因</el-button
              >
            </div>
            <small v-if="row.analysis_summary" :title="row.analysis_summary">{{ row.analysis_summary }}</small>
          </div>
          <span v-else class="heal-none">—</span></template
        ></el-table-column
      >
      <el-table-column label="最近发生" width="156"
        ><template #default="{ row }"
          ><div class="event-time">
            <strong>{{ fmtDate(row.last_seen_at) }}</strong
            ><small>{{ fmtClock(row.last_seen_at) }}</small>
          </div></template
        ></el-table-column
      >
      <el-table-column label="状态" width="132"
        ><template #default="{ row }"
          ><div class="event-status">
            <template v-if="row.status === 'open'"
              ><el-button v-if="!row.snoozed" link type="warning" :loading="ackBusy === row.id" @click="ackEvent(row)"
                >已知晓</el-button
              ><el-tag v-else type="info" effect="plain" round :title="row.snoozed_by_name || '已静音'"
                >已知晓 · 不再提醒</el-tag
              > </template
            ><el-tag v-else type="success" effect="plain" round
              ><span v-if="row.snoozed">已恢复 · 曾已知晓</span><span v-else>已恢复</span></el-tag
            >
          </div></template
        ></el-table-column
      >
    </el-table>
    <el-empty v-if="!eventsLoading && events.length === 0" description="暂无告警事件" />
    <div v-if="eventTotal > 0" class="event-pagination">
      <el-pagination
        v-model:current-page="eventPage"
        v-model:page-size="eventPageSize"
        :total="eventTotal"
        :page-sizes="[20, 50, 100, 200]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="fetchEvents()"
        @size-change="onPageSizeChange"
      />
    </div>

    <el-dialog
      v-model="analysisVisible"
      title="AI 归因分析"
      width="720px"
      class="rule-dialog analysis-dialog"
      :close-on-click-modal="false"
    >
      <div v-loading="analysisLoading" class="analysis-body">
        <div v-if="analysisEvent" class="analysis-meta">
          <strong>{{ analysisEvent.rule_name }}</strong
          ><small
            >{{ analysisEvent.device_name }} · {{ analysisEvent.device_ip || analysisEvent.resource_name || '-' }} ·
            {{ fmtDate(analysisEvent.last_seen_at) }} {{ fmtClock(analysisEvent.last_seen_at) }}</small
          >
        </div>
        <pre class="analysis-text">{{ analysisText || '暂无归因结论，可点击右下角重新分析' }}</pre>
      </div>
      <template #footer
        ><el-button @click="analysisVisible = false">关闭</el-button
        ><el-button type="primary" :loading="healBusy === analysisEvent?.id" @click="rerunAnalysis"
          >重新分析</el-button
        ></template
      >
    </el-dialog>
    <MaintenanceWindows ref="maintenanceRef" />
  </section>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { Mute, Refresh, Search, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { alertAPI } from '@/api'
import MaintenanceWindows from '@/components/panel/alerts/MaintenanceWindows.vue'
import type { AlertEvent } from '@/types'
import {
  analysisLabel,
  analysisTag,
  canAnalyze,
  canRemediate,
  errText,
  fmtClock,
  fmtDate,
  healHint,
  notifyLabel,
  remediationLabel,
  remediationTag,
  severityLabel,
  severityType,
} from './alertShared'

const eventsLoading = ref(false)
const events = ref<AlertEvent[]>([])
const eventStatus = ref('open')
const eventSeverity = ref('')
/** 历史事件检索(90 天保留):指标/关键字/时间窗口 + 分页 */
const eventMetric = ref('')
const eventSearch = ref('')
const eventRange = ref<[Date, Date] | null>(null)
const eventPage = ref(1)
const eventPageSize = ref(50)
const eventTotal = ref(0)
const rangeShortcuts = [
  {
    text: '最近 24 小时',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setHours(start.getHours() - 24)
      return [start, end]
    },
  },
  {
    text: '最近 7 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 7)
      return [start, end]
    },
  },
  {
    text: '最近 30 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 30)
      return [start, end]
    },
  },
]
const maintenanceRef = ref<InstanceType<typeof MaintenanceWindows>>()

async function fetchEvents(quiet = false) {
  if (!quiet) eventsLoading.value = true
  try {
    const params: Record<string, string | number | undefined> = {
      status: eventStatus.value || undefined,
      severity: eventSeverity.value || undefined,
      metric: eventMetric.value || undefined,
      q: eventSearch.value.trim() || undefined,
      page: eventPage.value,
      page_size: eventPageSize.value,
    }
    const range = eventRange.value
    if (Array.isArray(range) && range.length === 2) {
      const [from, to] = range
      if (from) params.since = new Date(from).toISOString()
      if (to) params.until = new Date(to).toISOString()
    }
    const data = (await alertAPI.events(params)).data
    events.value = data.items
    eventTotal.value = data.total
    // 筛选后当前页超出总页数时(如换筛选后仍在第 3 页),回到第 1 页重拉
    const maxPage = Math.max(1, Math.ceil(eventTotal.value / eventPageSize.value))
    if (eventPage.value > maxPage) {
      eventPage.value = maxPage
      await fetchEvents(quiet)
    }
  } catch {
    if (!quiet) ElMessage.error('加载告警事件失败')
  } finally {
    if (!quiet) eventsLoading.value = false
  }
}
/** 筛选条件变化:回到第 1 页重新拉取。 */
function onFilterChange() {
  eventPage.value = 1
  void fetchEvents()
}
/** 关键字搜索防抖 400ms:每个字符都发请求会打爆后端。 */
let searchDebounce: number | undefined
function onSearchInput() {
  if (searchDebounce !== undefined) window.clearTimeout(searchDebounce)
  searchDebounce = window.setTimeout(() => {
    searchDebounce = undefined
    onFilterChange()
  }, 400)
}
function onPageSizeChange() {
  eventPage.value = 1
  void fetchEvents()
}
function resetEventFilters() {
  eventStatus.value = 'open'
  eventSeverity.value = ''
  eventMetric.value = ''
  eventSearch.value = ''
  eventRange.value = null
  onFilterChange()
}
/** 30s 主轮询/页签切换时的静默刷新入口(父级调用,不转 loading) */
function refreshQuiet() {
  return fetchEvents(true)
}

const healBusy = ref(0)
/** 已知晓:恢复前不再重复提醒(评估/归因照常) */
const ackBusy = ref(0)
async function ackEvent(row: AlertEvent) {
  ackBusy.value = row.id
  try {
    await alertAPI.ackEvent(row.id)
    ElMessage.success('已知晓，该告警恢复前不再重复提醒')
    await fetchEvents(true)
  } catch {
    ElMessage.error('操作失败，请稍后重试')
  } finally {
    ackBusy.value = 0
  }
}
async function remediate(row: AlertEvent) {
  healBusy.value = row.id
  try {
    await alertAPI.remediateEvent(row.id)
    ElMessage.success('已下发拉起指令，正在跟踪恢复状态')
    await fetchEvents(true)
  } catch (e) {
    ElMessage.error(errText(e, '自动处置失败'))
  } finally {
    healBusy.value = 0
  }
}
async function analyze(row: AlertEvent) {
  healBusy.value = row.id
  try {
    await alertAPI.analyzeEvent(row.id)
    ElMessage.success('已触发 AI 归因分析，结论出来后会自动刷新')
    await fetchEvents(true)
    startAnalysisListPolling(row.id)
  } catch (e) {
    ElMessage.error(errText(e, 'AI 归因分析失败'))
  } finally {
    healBusy.value = 0
  }
}

const analysisVisible = ref(false)
const analysisLoading = ref(false)
const analysisEvent = ref<AlertEvent | null>(null)
const analysisText = ref('')
const analysisTimerIds = new Set<number>()

/**
 * 归因轮询统一实现:长耗时任务,列表(盯行状态)/弹窗(拉详情)共用同一时钟。
 * 页面不可见跳过;超过 maxPolls 停;回调返回 false 也停。
 */
function startAnalysisWatch(intervalMs: number, maxPolls: number, onPoll: () => Promise<boolean>): number {
  let polls = 0
  const timer = window.setInterval(async () => {
    if (document.hidden) return
    if (++polls > maxPolls) {
      window.clearInterval(timer)
      analysisTimerIds.delete(timer)
      return
    }
    const keep = await onPoll().catch(() => true)
    if (!keep) {
      window.clearInterval(timer)
      analysisTimerIds.delete(timer)
    }
  }, intervalMs)
  analysisTimerIds.add(timer)
  return timer
}

function stopAllAnalysisWatchers() {
  for (const timer of analysisTimerIds) window.clearInterval(timer)
  analysisTimerIds.clear()
}

/**
 * 列表定向盯梢:结论通常十几秒就出,默认 30s 整体刷新等不了那么久。
 * 每 5s 盯这一条,落到终态就停;迟迟没进入 running(闸门拒绝/提交失败)也不空等。
 */
function startAnalysisListPolling(eventId: number) {
  stopAllAnalysisWatchers()
  startAnalysisWatch(5000, 180, async () => {
    await fetchEvents(true)
    const row = events.value.find((item) => item.id === eventId)
    if (!row) return false
    const state = row.analysis_state || ''
    if (state === 'completed' || state === 'failed' || state === 'skipped') return false
    return state !== ''
  })
}
/** 归因是长耗时任务:弹窗打开时每 10s 拉一次，最多 15 分钟,页面不可见则跳过 */
function startAnalysisPolling() {
  stopAllAnalysisWatchers()
  startAnalysisWatch(10000, 90, async () => {
    const running = await refreshAnalysis()
    return running
  })
}
/** 拉一次归因详情;返回是否仍在分析中(用于决定要不要继续轮询) */
async function refreshAnalysis() {
  const row = analysisEvent.value
  if (!row) return false
  try {
    const detail = (await alertAPI.event(row.id)).data
    analysisEvent.value = detail
    analysisText.value =
      detail.analysis_text ||
      detail.analysis_summary ||
      (detail.analysis_state === 'running' ? 'AI 归因分析进行中…' : '')
    if (detail.analysis_state !== 'running') {
      stopAllAnalysisWatchers()
      await fetchEvents(true)
    }
    return detail.analysis_state === 'running'
  } catch {
    return false
  }
}
async function openAnalysis(row: AlertEvent) {
  analysisEvent.value = row
  analysisText.value = row.analysis_text || row.analysis_summary || ''
  analysisVisible.value = true
  analysisLoading.value = true
  stopAllAnalysisWatchers()
  const running = await refreshAnalysis()
  analysisLoading.value = false
  if (running) startAnalysisPolling()
}
async function rerunAnalysis() {
  const row = analysisEvent.value
  if (!row) return
  healBusy.value = row.id
  try {
    await alertAPI.analyzeEvent(row.id)
    analysisText.value = 'AI 归因分析进行中…'
    startAnalysisPolling()
    await fetchEvents(true)
  } catch (e) {
    ElMessage.error(errText(e, 'AI 归因分析失败'))
  } finally {
    healBusy.value = 0
  }
}

watch(analysisVisible, (open) => {
  if (!open) stopAllAnalysisWatchers()
})
onUnmounted(() => {
  if (searchDebounce !== undefined) window.clearTimeout(searchDebounce)
  stopAllAnalysisWatchers()
})

defineExpose({ refreshQuiet })
</script>

<style scoped>
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
.event-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.event-filters .el-select {
  width: 120px;
}
.event-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.event-main {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.event-main small,
.cell-sub {
  color: var(--dcn-text-secondary);
  font-size: 12px;
  white-space: normal;
}
.cell-sub {
  display: block;
  margin-top: 3px;
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
.object-cell,
.event-copy,
.event-time,
.event-status {
  display: flex;
  flex-direction: column;
}
.object-cell strong {
  color: var(--dcn-text-primary);
  font-size: 13px;
  font-weight: 650;
}
.object-cell small,
.event-copy small {
  color: var(--dcn-text-secondary);
  font-size: 11px;
  line-height: 1.45;
  /* 业务的异常明细等消息自带换行,按真实换行渲染(2026-09-18) */
  white-space: pre-line;
}
.event-severity {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  flex: 0 0 auto;
  box-sizing: border-box;
  height: 24px;
  padding: 0 7px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 650;
  line-height: 1;
  vertical-align: middle;
}
.event-main .event-severity {
  flex-direction: row;
  min-width: auto;
}
.event-severity .el-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  margin: 0;
  line-height: 1;
  vertical-align: middle;
}
.event-severity .el-icon svg {
  display: block;
  width: 12px;
  height: 12px;
}
.event-severity.warning {
  background: color-mix(in srgb, var(--dcn-warning) 13%, transparent);
  color: var(--dcn-warning);
}
.event-severity.danger {
  background: color-mix(in srgb, var(--dcn-danger) 13%, transparent);
  color: var(--dcn-danger);
}
.event-severity.info {
  background: color-mix(in srgb, var(--dcn-info) 13%, transparent);
  color: var(--dcn-info);
}
.event-copy {
  gap: 4px;
  min-width: 0;
}
.event-copy strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.event-copy small {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  white-space: normal;
  -webkit-line-clamp: 2;
}
.object-cell {
  gap: 4px;
}
.event-time {
  align-items: flex-start;
  gap: 2px;
  padding-left: 10px;
  border-left: 2px solid color-mix(in srgb, var(--dcn-primary) 38%, transparent);
}
.event-time strong {
  color: var(--dcn-text-primary);
  font: 600 12px var(--dcn-font-mono);
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.event-time small {
  color: var(--dcn-primary-light);
  font: 600 13px var(--dcn-font-mono);
}
.event-status {
  align-items: flex-start;
}
.heal-cell {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.heal-tag-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.heal-cell small {
  overflow: hidden;
  color: var(--dcn-text-secondary);
  font-size: 11px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.heal-none {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.heal-tag-row :deep(.el-button.is-link) {
  height: 22px;
  padding: 0;
  font-size: 12px;
}
.analysis-body {
  min-height: 200px;
}
.analysis-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 12px;
}
.analysis-meta strong {
  color: var(--dcn-text-primary);
  font-size: 14px;
}
.analysis-meta small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.analysis-text {
  max-height: 52vh;
  margin: 0;
  padding: 14px 16px;
  overflow: auto;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
  font: 12px/1.75 var(--dcn-font-mono);
  white-space: pre-wrap;
  word-break: break-word;
}
@media (max-width: 900px) {
  .alert-table :deep(.el-table__body-wrapper) {
    overflow-x: auto;
  }
  .event-time {
    padding-left: 7px;
  }
}
@media (max-width: 600px) {
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
  .event-filters {
    width: 100%;
    flex-wrap: wrap;
  }
}
</style>
