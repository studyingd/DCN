<template>
  <el-drawer
    v-model="visible"
    size="min(960px, 88vw)"
    direction="rtl"
    :title="job?.name || '任务详情'"
    @closed="onClosed"
  >
    <div v-loading="loading" class="job-detail">
      <template v-if="job">
        <div class="detail-summary">
          <el-tag :type="jobStatusTagType(job.status)" size="large" effect="plain">{{
            jobStatusLabel(job.status)
          }}</el-tag>
          <span>{{ jobTypeMeta[job.job_type].label }}</span>
          <span>{{ job.targets.length }} 台设备</span>
          <span>{{ formatDateTime(job.created_at, true) }}</span>
        </div>
        <div v-if="job.job_type === 'agent' && job.config_json?.question" class="detail-question">
          <span class="detail-question-label">诊断问题</span>
          <p>{{ job.config_json.question }}</p>
        </div>
        <el-progress :percentage="detailProgress" :status="progressStatus(job.status)" :stroke-width="10" />
        <div class="target-list">
          <div v-for="target in job.targets" :key="target.id" class="target-card">
            <div class="target-head">
              <span class="status-dot" :class="`status-${target.status}`" /><span
                ><strong>{{ target.device_name }}</strong
                ><small>{{ target.device_ip || '-' }}</small></span
              ><el-tag size="small" :type="targetStatusTagType(target.status)" effect="plain">{{
                targetStatusLabel(target.status)
              }}</el-tag
              ><span class="target-duration">{{
                target.duration_ms != null ? fmtDuration(target.duration_ms) : ''
              }}</span>
            </div>
            <!-- agent 任务:跳转该目标的完整诊断时间线(取证步骤/动作/审批与报告) -->
            <el-button
              v-if="job.job_type === 'agent' && target.result_json?.legacy_run_id"
              link
              type="primary"
              size="small"
              class="timeline-link"
              @click="openTimeline(Number(target.result_json.legacy_run_id))"
              >查看诊断时间线</el-button
            >
            <!-- 巡检结果统一交给 InspectionResultCard（状态点 + 中文标签 + 数值 + 原始输出弹窗），
                 不再把 step 的英文键和命令原文直接糊在抽屉里 -->
            <template v-if="job.job_type === 'inspection'">
              <InspectionResultCard v-if="inspectionResults[target.id]" :result="inspectionResults[target.id]" />
              <!-- 巡检记录还没拉到（或拉取失败）时用 result_json 里现成的计数兜底；
                   卡片就绪后只剩卡片头部的计数，两处不会同时出现 -->
              <div v-else-if="toSummaryCells(target.result_json).length" class="inspection-summary">
                <span v-for="cell in toSummaryCells(target.result_json)" :key="cell.text" :class="cell.tone">
                  {{ cell.text }}
                </span>
              </div>
            </template>
            <!-- 执行明细：step.output 是命令原文，只适合排障。
                 巡检任务在结果卡片就绪时隐藏它——卡片每项已有「原始输出」弹窗，
                 两个入口内容重叠、反而困惑；只在卡片拉取失败时保留作兑底。
                 非巡检任务（agent/script/power）没有结果卡片，始终保留。 -->
            <el-collapse
              v-if="target.steps.length && (job.job_type !== 'inspection' || !inspectionResults[target.id])"
              v-model="expandedStepPanels"
              class="step-collapse"
            >
              <el-collapse-item :name="`target-${target.id}`" title="查看执行明细">
                <div class="step-list">
                  <div v-for="step in target.steps" :key="step.id" class="step-row">
                    <el-icon :class="step.status === 'failed' ? 'step-failed' : 'step-ok'"
                      ><CircleCheck v-if="step.status !== 'failed'" /><CircleClose v-else /></el-icon
                    ><span class="step-name">{{ step.step_name }}</span
                    ><code v-if="step.exit_code != null">exit {{ step.exit_code }}</code>
                    <el-collapse v-if="step.output || step.error_message" class="step-output"
                      ><el-collapse-item title="查看输出">
                        <pre :class="{ error: step.error_message }">{{ step.error_message || step.output }}</pre>
                      </el-collapse-item></el-collapse
                    >
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
            <div
              v-if="target.result_json?.report"
              class="agent-report"
              v-html="renderMarkdownCached(String(target.result_json.report))"
            />
            <el-alert
              v-if="target.error_message && !target.steps.length"
              type="error"
              :title="target.error_message"
              :closable="false"
              show-icon
            />
          </div>
        </div>
      </template>
    </div>
    <!-- Agent 诊断时间线抽屉:job 形态的结果里带 legacy_run_id,与实时形态同一渲染 -->
    <TimelineDrawer ref="timelineRef" />
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, CircleClose } from '@element-plus/icons-vue'
import { automationAPI, inspectionAPI } from '@/api'
import { usePolling } from '@/composables/usePolling'
import {
  jobStatusLabel,
  jobStatusTagType,
  progressStatus,
  targetStatusLabel,
  targetStatusTagType,
} from '@/utils/agentRunText'
import { targetsProgress } from '@/utils/automationProgress'
import { formatDateTime } from '@/utils/datetime'
import { toDeviceResult, toSummaryCells } from '@/utils/inspectionAdapter'
import { renderMarkdownCached } from '@/utils/sanitize'
import InspectionResultCard from '../InspectionResultCard.vue'
import TimelineDrawer from './TimelineDrawer.vue'
import { jobTypeMeta } from './jobTypes'
import type { AutomationJob, AutomationTarget } from '@/types/automation'
import type { InspectionDeviceResult } from '@/types/inspection'

/**
 * 任务详情抽屉:id 驱动,open(id) 打开;pending/running 期间 1.5s 轮询,
 * 终态后 emit('job-finished') 让父壳刷新列表/统计。
 */
const emit = defineEmits<{ (e: 'job-finished'): void }>()

const visible = ref(false)
const loading = ref(false)
const job = ref<AutomationJob | null>(null)
/** 防竞态:打开新任务时,旧任务的迟到响应不再覆盖 */
let currentId: number | null = null

const detailProgress = computed(() => (job.value ? targetsProgress(job.value.targets) : 0))
// target.id → 适配后的巡检结果，交给 InspectionResultCard 渲染
const inspectionResults = ref<Record<number, InspectionDeviceResult>>({})
// 已发起过详情请求的巡检记录 id：轮询期间靠它避免重复拉取
const requestedRecordIds = new Set<number>()
// 「执行明细」折叠面板的展开项
const expandedStepPanels = ref<string[]>([])

const timelineRef = ref<InstanceType<typeof TimelineDrawer>>()

const poll = usePolling(() => load(currentId ?? -1), { intervalMs: 1500 })

function recordIdOf(target: AutomationTarget) {
  return Number(target.result_json?.record_id ?? 0)
}

/**
 * 巡检任务：按 target.result_json.record_id 拉取完整巡检记录，转成卡片需要的形状。
 * 多台设备并发拉取；单台失败只影响它自己（回退到汇总条 + 执行明细），不会让整个抽屉白屏。
 */
async function loadInspectionResults(current: AutomationJob) {
  if (current.job_type !== 'inspection') return
  const pending = current.targets.filter((target) => {
    const recordId = recordIdOf(target)
    return recordId > 0 && !requestedRecordIds.has(recordId)
  })
  if (!pending.length) return
  for (const target of pending) requestedRecordIds.add(recordIdOf(target))
  const settled = await Promise.allSettled(
    pending.map(async (target) => ({
      targetId: target.id,
      record: (await inspectionAPI.getRecordDetail(recordIdOf(target))).data,
    })),
  )
  const fetched: Record<number, InspectionDeviceResult> = {}
  for (const item of settled) {
    if (item.status === 'fulfilled') fetched[item.value.targetId] = toDeviceResult(item.value.record)
  }
  // 合并而不是覆盖：轮询期间已经拉到的结果不能被冲掉
  if (Object.keys(fetched).length) inspectionResults.value = { ...inspectionResults.value, ...fetched }
}

/**
 * 执行明细的默认展开状态：巡检任务的 step.output 是命令原文，可读结果已由卡片呈现，
 * 默认收起；脚本 / 电源 / Agent 任务的 step 本身就是主要结果，默认展开。
 * 只在打开抽屉时设置一次，轮询刷新不会覆盖用户手动改过的展开状态。
 */
function syncStepPanels() {
  const current = job.value
  expandedStepPanels.value =
    current && current.job_type !== 'inspection' ? current.targets.map((target) => `target-${target.id}`) : []
}

async function load(id: number) {
  try {
    const res = await automationAPI.job(id)
    if (currentId !== id) return // 已切换到其它任务,丢弃迟到响应
    job.value = res.data
    void loadInspectionResults(res.data)
    if (['pending', 'running'].includes(res.data.status)) poll.start()
    else {
      poll.stop()
      emit('job-finished')
    }
  } catch {
    if (currentId !== id) return
    ElMessage.error('加载任务详情失败')
    poll.stop()
  }
}

async function open(id: number) {
  poll.stop()
  currentId = id
  visible.value = true
  loading.value = true
  // 切换任务时清掉上一个任务的巡检结果缓存，避免抽屉里短暂串台
  inspectionResults.value = {}
  requestedRecordIds.clear()
  expandedStepPanels.value = []
  await load(id)
  loading.value = false
  syncStepPanels()
}

function onClosed() {
  poll.stop()
  currentId = null
}

function openTimeline(runId: number) {
  void timelineRef.value?.open(runId)
}

function fmtDuration(ms: number) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

defineExpose({ open })
</script>

<style scoped>
.job-detail {
  min-height: 280px;
}
.detail-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.detail-question {
  margin-bottom: 16px;
  padding: 12px 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
}
.detail-question-label {
  display: block;
  margin-bottom: 6px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.detail-question p {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.target-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 18px;
}
.target-card {
  padding: 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
}
.target-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.target-head > span:nth-child(2) {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}
.target-head small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.target-duration {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.status-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--dcn-text-disabled);
}
.status-completed {
  background: var(--dcn-success);
}
.status-running {
  background: var(--dcn-primary);
  box-shadow: 0 0 0 4px var(--dcn-primary-soft);
}
.status-failed {
  background: var(--dcn-danger);
}
.status-warning {
  background: var(--dcn-warning);
}
/* 巡检记录还没拉到时先用 result_json 里现成的计数顶上；卡片就绪后这一条不再渲染 */
.inspection-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 12px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.inspection-summary .sum-normal {
  color: var(--dcn-success);
}
.inspection-summary .sum-warning {
  color: var(--dcn-warning);
}
.inspection-summary .sum-critical {
  color: var(--dcn-danger);
}
/* 执行明细默认收起：step.output 是命令原文，只在排障时展开看 */
.step-collapse {
  margin-top: 10px;
  border: 0;
}
.step-collapse :deep(.el-collapse-item__header) {
  height: 32px;
  border: 0;
  background: transparent;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.step-collapse :deep(.el-collapse-item__wrap) {
  border: 0;
  background: transparent;
}
.step-list {
  margin: 12px 0 0 18px;
  padding-left: 14px;
  border-left: 1px solid var(--dcn-border-light);
}
.step-row {
  display: grid;
  grid-template-columns: 18px minmax(120px, auto) auto 1fr;
  gap: 8px;
  /* 图标(18px)与文字(13px)同行对齐:start 会让图标顶格、文字下沉一格 */
  align-items: center;
  padding: 8px 0;
}
.step-ok {
  color: var(--dcn-success);
}
.step-failed {
  color: var(--dcn-danger);
}
.step-name {
  font-size: 13px;
}
.step-row code {
  color: var(--dcn-text-secondary);
  font-size: 11px;
  font-family: var(--dcn-font-mono);
}
.step-output {
  grid-column: 2 / -1;
  border: 0;
}
.step-output :deep(.el-collapse-item__header) {
  height: 28px;
  border: 0;
  background: transparent;
  color: var(--dcn-primary);
  font-size: 12px;
}
.step-output :deep(.el-collapse-item__wrap) {
  border: 0;
  background: transparent;
}
.step-output pre {
  max-height: 240px;
  overflow: auto;
  padding: 10px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  color: var(--dcn-text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  font-family: var(--dcn-font-mono);
}
.step-output pre.error {
  color: var(--dcn-danger);
}
.agent-report {
  margin-top: 14px;
  padding: 14px;
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
  line-height: 1.65;
}
.timeline-link {
  margin: 4px 0 6px;
}
</style>
