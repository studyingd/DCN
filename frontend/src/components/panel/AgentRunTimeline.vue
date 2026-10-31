<template>
  <div v-if="run" class="run-timeline">
    <div class="run-head">
      <div>
        <strong>#{{ run.id }} · {{ run.device_name }}</strong>
        <small>{{ run.question }}</small>
      </div>
      <div class="run-meta">
        <el-tag v-if="waitingApproval" type="warning" effect="dark">等待审批</el-tag>
        <el-tag :type="runStatusTagType(run.status)" effect="plain">{{ runStatusText(run.status) }}</el-tag>
        <el-tag effect="plain" size="small">{{ actionModeText(run.mode) }}</el-tag>
        <small v-if="run.duration_ms">{{ (run.duration_ms / 1000).toFixed(1) }}s</small>
      </div>
    </div>

    <div v-if="run.steps.length" class="steps">
      <template v-for="(step, idx) in visibleSteps" :key="idx">
        <!-- 变更动作卡 -->
        <div v-if="step.tool === 'run_action'" class="action-card" :class="{ pending: isPending(step) }">
          <div class="action-head">
            <el-tag
              :type="step.key === 'custom_read' ? 'primary' : step.risk === 'l1' ? 'success' : 'danger'"
              size="small"
              effect="dark"
            >
              {{ step.key === 'custom_read' ? '审批读' : step.risk === 'l1' ? 'L1 拉起' : 'L2 变更' }}
            </el-tag>
            <strong>{{ actionTitle(step) }}</strong>
            <el-tag :type="actionFinalTagType(step.final)" size="small" effect="plain">
              {{ actionFinalText(step.final) }}
            </el-tag>
            <small v-if="step.auto" class="auto-flag">自动执行</small>
          </div>
          <div class="action-reason">理由:{{ step.reason }}</div>
          <!-- 命令原文默认展开(2026-10-30 用户定调,不再折叠):审批人第一眼
               就能对上"批的=执行的"字符串 -->
          <div class="action-cmd">
            <small>命令原文</small>
            <code>{{ step.command }}</code>
          </div>
          <div v-if="isPending(step)" class="approve-bar">
            <template v-if="canManage">
              <el-input
                v-model="approvalNotes[step.approval_id ?? 0]"
                size="small"
                placeholder="备注(可选)"
                class="note-input"
              />
              <el-button type="success" size="small" :loading="deciding" @click="decide(step, true)"> 批准 </el-button>
              <el-button type="danger" size="small" :loading="deciding" @click="decide(step, false)"> 拒绝 </el-button>
              <ApprovalCountdown
                v-if="step.approval_id != null"
                :approvals="run.approvals"
                :approval-id="step.approval_id"
              />
            </template>
            <small v-else>等待有 automation:manage 权限的用户审批</small>
          </div>
          <div v-if="step.decision_note" class="decision-note">审批备注:{{ step.decision_note }}</div>
          <pre v-if="step.preview" class="step-preview">{{ step.preview }}</pre>
          <div v-if="step.error" class="step-error">{{ step.error }}</div>
        </div>
        <!-- 常规步骤行 -->
        <div v-else class="step-row">
          <span class="step-dot" :class="step.ok ? 'ok' : 'fail'">{{ step.ok ? '✓' : '✕' }}</span>
          <span class="step-name">{{ stepName(step) }}</span>
          <small v-if="step.duration_ms != null" class="step-duration"
            >{{ (step.duration_ms / 1000).toFixed(1) }}s</small
          >
          <small v-if="step.truncated" class="truncated-flag">已截断</small>
          <code v-if="step.command" class="step-cmd">{{ step.command }}</code>
          <pre v-if="step.preview" class="step-preview">{{ step.preview }}</pre>
          <div v-if="step.error" class="step-error">{{ step.error }}</div>
        </div>
      </template>
    </div>

    <div v-if="run.error" class="run-error">{{ run.error }}</div>
    <!-- 报告与任务详情抽屉同一渲染口径(经 sanitize 的 markdown,不再裸铺 ** 星号)。
         用缓存版:轮询每轮整体替换 run 对象,内联渲染会把整段 markdown 重新
         parse+sanitize,是抽屉打开/刷新掉帧的帮凶之一 -->
    <div v-if="run.report" class="agent-report" v-html="renderMarkdownCached(run.report)" />
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { agentAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { usePendingApprovals } from '@/composables/usePendingApprovals'
import {
  actionFinalTagType,
  actionFinalText,
  actionModeText,
  runStatusTagType,
  runStatusText,
} from '@/utils/agentRunText'
import { renderMarkdownCached } from '@/utils/sanitize'
import type { AgentRunDetail, AgentStep } from '@/types/agent'
import ApprovalCountdown from './ApprovalCountdown.vue'

/**
 * Agent 诊断运行时间线(只读展示 + 待审批动作的批准/拒绝)。
 * 智能诊断页与任务详情抽屉共用;run 数据由父组件拉取与轮询,
 * 决策后 emit('updated') 通知父组件刷新。渲染开销控制:报告走
 * renderMarkdownCached;倒计时独立成 ApprovalCountdown(1s tick
 * 不再重渲染整条时间线)。
 */
const props = defineProps<{ run: AgentRunDetail | null }>()
const emit = defineEmits<{ updated: [] }>()

const authStore = useAuthStore()
const { refresh: refreshPendingApprovals } = usePendingApprovals()
const canManage = computed(() => authStore.hasPermission('automation:manage'))
const deciding = ref(false)
const approvalNotes = reactive<Record<number, string>>({})

const visibleSteps = computed(() => (props.run?.steps || []).filter((s) => s.tool !== 'llm_usage'))
const waitingApproval = computed(() => (props.run?.approvals || []).some((a) => a.status === 'pending'))

function stepName(step: AgentStep) {
  if (step.tool === 'get_metrics') return `平台指标趋势(近 ${step.hours || 1}h)`
  return step.label ? `${step.label}(${step.key})` : step.key || step.tool
}
function actionTitle(step: AgentStep) {
  const p = step.params || {}
  const target = (p.name as string) || (p.unit as string) || (p.command as string) || ''
  return `${step.label || step.key}${target ? `: ${target}` : ''}`
}

function isPending(step: AgentStep) {
  if (step.final !== 'pending' || !step.approval_id) return false
  const a = props.run?.approvals?.find((x) => x.id === step.approval_id)
  return a?.status === 'pending'
}

async function decide(step: AgentStep, approve: boolean) {
  const id = step.approval_id
  if (!id) return
  deciding.value = true
  try {
    const note = approvalNotes[id] || ''
    if (approve) await agentAPI.approveAction(id, note)
    else await agentAPI.rejectAction(id, note)
    ElMessage.success(approve ? '已批准,诊断线程将执行该动作' : '已拒绝')
    void refreshPendingApprovals() // 顶栏徽标/概览卡计数立即收敛
    emit('updated')
  } catch (e) {
    ElMessage.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '操作失败')
  } finally {
    deciding.value = false
  }
}
</script>

<style scoped>
.run-timeline {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--dcn-text-regular);
}
.run-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 8px;
}
.run-head strong {
  color: var(--dcn-text-primary);
}
.run-head small {
  display: block;
  color: var(--dcn-text-secondary);
  margin-top: 4px;
}
.run-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}
.step-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-muted);
  font-size: 13px;
  flex-wrap: wrap;
}
.step-dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  flex-shrink: 0;
}
.step-dot.ok {
  background: var(--dcn-success-bg);
  color: var(--dcn-success);
}
.step-dot.fail {
  background: var(--dcn-danger-bg);
  color: var(--dcn-danger);
}
.step-name {
  font-weight: 600;
  color: var(--dcn-text-primary);
}
.truncated-flag {
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
/* 单步耗时(run 1056 复盘):定位慢项用 */
.step-duration {
  font-size: 11px;
  color: var(--dcn-text-secondary);
  flex-shrink: 0;
}
/* 执行指令原文(2026-10-30):服务端白名单拼装的命令随步骤落库,只读取证过程可见 */
.step-cmd {
  width: 100%;
  margin: 2px 0 0;
  padding: 4px 8px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  color: var(--dcn-text-secondary);
  font-size: 11px;
  font-family: var(--dcn-font-mono);
  white-space: pre-wrap;
  word-break: break-all;
}
.step-preview {
  width: 100%;
  margin: 4px 0 0;
  padding: 6px 8px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  color: var(--dcn-text-regular);
  font-size: 12px;
  font-family: var(--dcn-font-mono);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}
.step-error {
  width: 100%;
  color: var(--dcn-danger);
  font-size: 12px;
}
.action-card {
  border: 1px solid color-mix(in srgb, var(--dcn-warning) 35%, transparent);
  border-radius: var(--dcn-radius-lg);
  padding: 10px 12px;
  font-size: 13px;
}
.action-card.pending {
  border-color: color-mix(in srgb, var(--dcn-warning) 80%, transparent);
  box-shadow: 0 0 0 1px var(--dcn-warning-bg);
  background: var(--dcn-warning-bg);
}
.action-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.action-head strong {
  color: var(--dcn-text-primary);
}
.auto-flag {
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
.action-reason {
  margin: 6px 0 2px;
  color: var(--dcn-text-regular);
}
.action-cmd {
  margin: 4px 0;
  padding: 6px 8px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
}
.action-cmd small {
  display: block;
  margin-bottom: 2px;
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
.action-cmd code {
  font-size: 12px;
  font-family: var(--dcn-font-mono);
  word-break: break-all;
  white-space: pre-wrap;
  color: var(--dcn-primary-light);
}
.approve-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.note-input {
  width: 220px;
}
.decision-note {
  margin-top: 6px;
  font-size: 12px;
  color: var(--dcn-text-secondary);
}
.run-error {
  color: var(--dcn-danger);
  margin-bottom: 8px;
  font-size: 13px;
}
.agent-report {
  padding: 14px;
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
  color: var(--dcn-text-regular);
  font-size: 13px;
  line-height: 1.7;
  /* 结论区高度跟视口走:固定 420px 在长报告下要滚很久(2026-10-30
     用户反馈加高;宽度维持抽屉 60% 不动) */
  max-height: 62vh;
  overflow: auto;
}
.agent-report :deep(h1),
.agent-report :deep(h2),
.agent-report :deep(h3),
.agent-report :deep(h4) {
  color: var(--dcn-text-primary);
  margin: 12px 0 6px;
}
.agent-report :deep(h1:first-child),
.agent-report :deep(h2:first-child) {
  margin-top: 0;
}
.agent-report :deep(code) {
  padding: 1px 5px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  font-family: var(--dcn-font-mono);
  font-size: 12px;
}
.agent-report :deep(pre) {
  padding: 10px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  overflow: auto;
}
.agent-report :deep(pre code) {
  padding: 0;
  background: transparent;
}
.agent-report :deep(table) {
  border-collapse: collapse;
}
.agent-report :deep(th),
.agent-report :deep(td) {
  border: 1px solid var(--dcn-border);
  padding: 4px 8px;
}
</style>
