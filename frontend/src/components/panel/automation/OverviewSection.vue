<template>
  <section class="automation-page">
    <div class="summary-grid">
      <button v-if="pendingApprovals > 0" type="button" class="summary-card" @click="emit('open-approvals')">
        <span class="atm-icon-box summary-icon atm-icon--warning"
          ><el-icon><Bell /></el-icon
        ></span>
        <span
          ><strong>{{ pendingApprovals }}</strong
          ><small>Agent 动作待审批</small></span
        >
      </button>
      <button
        v-for="item in summaryCards"
        :key="item.status"
        type="button"
        class="summary-card"
        @click="emit('open-history', item.status)"
      >
        <span class="atm-icon-box summary-icon" :class="`atm-icon--${item.tone}`"
          ><el-icon><component :is="item.icon" /></el-icon
        ></span>
        <span
          ><strong>{{ item.value }}</strong
          ><small>{{ item.label }}</small></span
        >
      </button>
    </div>

    <div class="workbench-grid">
      <div class="atm-card">
        <div class="atm-card-heading">
          <div><strong>开始一次运维任务</strong><small>根据目标选择合适的执行能力</small></div>
        </div>
        <div class="atm-type-grid">
          <!-- Agent 分析诊断直入卡(2026-10-30 用户定调:排四个功能模块之首):Agent
               诊断的唯一发起入口(与其它类型同款弹窗交互);定时在诊断完成后的时间线里
               「存为定时计划」 -->
          <button type="button" class="atm-type-card" @click="emit('launch-agent')">
            <span class="atm-icon-box atm-type-icon atm-icon--primary"
              ><el-icon><ChatLineSquare /></el-icon
            ></span>
            <span class="atm-type-copy"
              ><strong>Agent 分析诊断</strong><small>单台实时取证与审批;可存为定时计划例行执行</small></span
            >
            <el-tag size="small" type="warning" effect="plain">实时</el-tag>
          </button>
          <button
            v-for="type in types"
            :key="type.key"
            type="button"
            class="atm-type-card"
            @click="emit('open-create', type.key)"
          >
            <span class="atm-icon-box atm-type-icon" :class="`atm-icon--${type.tone}`"
              ><el-icon><component :is="type.icon" /></el-icon
            ></span>
            <span class="atm-type-copy"
              ><strong>{{ type.label }}</strong
              ><small>{{ type.description }}</small></span
            >
            <el-tag
              size="small"
              :type="type.readOnly ? 'success' : type.key === 'power' ? 'danger' : 'warning'"
              effect="plain"
            >
              {{ type.readOnly ? '只读' : type.key === 'power' ? '严重风险' : '高风险' }}
            </el-tag>
          </button>
        </div>
      </div>

      <!-- 最新任务:统一展示全部执行记录(2026-10-30 起 Agent 分析诊断发起时
           同步落任务记录,四类功能在同一入口可见;详情抽屉可跳转诊断时间线) -->
      <div class="atm-card">
        <div class="atm-card-heading">
          <div><strong>最新任务</strong><small>自动化任务与 Agent 诊断统一展示</small></div>
          <el-button link type="primary" @click="emit('open-history')">查看全部</el-button>
        </div>
        <div v-loading="loading" class="recent-list">
          <button
            v-for="item in recentEntries"
            :key="item.key"
            type="button"
            class="recent-item"
            @click="emit('open-detail', item.id)"
          >
            <span class="atm-icon-box atm-job-type-icon" :class="`atm-icon--${item.tone}`"
              ><el-icon><component :is="item.icon" /></el-icon
            ></span>
            <span class="recent-main"
              ><strong>{{ item.title }}</strong
              ><small>{{ item.subtitle }}</small>
            </span>
            <el-tag size="small" :type="item.tagType" effect="plain">{{ item.tagLabel }}</el-tag>
          </button>
          <el-empty v-if="!loading && recentEntries.length === 0" description="暂无执行记录" :image-size="56" />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, type Component } from 'vue'
import { Bell, CircleCheck, CircleClose, Loading, Warning, ChatLineSquare } from '@element-plus/icons-vue'
import { usePendingApprovals } from '@/composables/usePendingApprovals'
import { jobStatusLabel, jobStatusTagType } from '@/utils/agentRunText'
import { jobTypeMeta, type JobTypeMeta, type JobTypeTone } from './jobTypes'
import type { AutomationJobListItem, AutomationJobType } from '@/types/automation'
import './automation.css'

const props = defineProps<{
  stats: { running: number; completed: number; partial: number; failed: number }
  recentJobs: AutomationJobListItem[]
  loading: boolean
  types: JobTypeMeta[]
}>()

const emit = defineEmits<{
  (e: 'open-history', status?: string): void
  (e: 'open-create', type?: AutomationJobType): void
  (e: 'launch-agent'): void
  (e: 'open-approvals'): void
  (e: 'open-detail', id: number): void
}>()

const { count: pendingApprovals } = usePendingApprovals()

const summaryCards = computed(() => [
  { status: 'running', label: '执行中任务', value: props.stats.running, tone: 'primary', icon: Loading },
  { status: 'completed', label: '已完成任务', value: props.stats.completed, tone: 'success', icon: CircleCheck },
  { status: 'partial', label: '部分异常', value: props.stats.partial, tone: 'warning', icon: Warning },
  { status: 'failed', label: '执行失败', value: props.stats.failed, tone: 'danger', icon: CircleClose },
])

function jobMetaFor(type: string) {
  return jobTypeMeta[type as keyof typeof jobTypeMeta]
}

/** 「最新任务」卡的行(实时诊断已落任务记录,四类功能统一走 job 数据源) */
interface RecentEntry {
  key: string
  id: number
  title: string
  subtitle: string
  tone: JobTypeTone
  icon: Component
  tagType: string
  tagLabel: string
}

const recentEntries = computed<RecentEntry[]>(() =>
  props.recentJobs.map((job) => {
    const meta = jobMetaFor(job.job_type)
    return {
      key: `job-${job.id}`,
      id: job.id,
      title: job.name,
      subtitle: `${meta.label} · ${job.target_total} 台 · ${fmtTime(job.created_at)}`,
      tone: meta.tone,
      icon: meta.icon,
      tagType: jobStatusTagType(job.status),
      tagLabel: jobStatusLabel(job.status),
    }
  }),
)

/** 当天只显时分,跨天补月-日 */
function fmtTime(value: string) {
  const d = new Date(value)
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}-${d.getDate()} ${hm}`
}
</script>

<style scoped>
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.summary-card {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 94px;
  padding: 18px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
  box-shadow: var(--dcn-shadow-sm);
  font: inherit;
}
.summary-card:hover {
  border-color: var(--dcn-border);
  transform: translateY(-1px);
}
.summary-card > span:last-child {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.summary-card strong {
  font-size: 26px;
  line-height: 1;
}
.summary-card small,
.recent-main small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.summary-icon {
  width: 46px;
  height: 46px;
  font-size: 22px;
}
.workbench-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr);
  gap: 16px;
  margin-top: 16px;
}
.recent-list {
  min-height: 200px;
}
.recent-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 2px;
  border: 0;
  border-bottom: 1px solid var(--dcn-border-light);
  background: transparent;
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
  font: inherit;
}
.recent-item:last-child {
  border-bottom: 0;
}
.recent-item:hover .recent-main strong {
  color: var(--dcn-primary);
}
.recent-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.recent-main strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 1100px) {
  .summary-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .workbench-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 760px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
