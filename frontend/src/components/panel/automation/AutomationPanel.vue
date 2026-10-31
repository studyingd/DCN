<template>
  <div class="inline-panel automation-center">
    <div class="inline-panel-header automation-header">
      <div>
        <h2 class="inline-panel-title">自动化运维</h2>
      </div>
      <el-button type="primary" @click="openCreate()">
        <el-icon><Plus /></el-icon>新建任务
      </el-button>
    </div>

    <div class="automation-body">
      <div class="automation-tabs" role="tablist" aria-label="自动化运维导航">
        <button
          v-for="item in sections"
          :key="item.key"
          type="button"
          role="tab"
          :aria-selected="section === item.key"
          :class="{ active: section === item.key }"
          @click="section = item.key"
        >
          <el-icon><component :is="item.icon" /></el-icon>{{ item.label }}
        </button>
      </div>

      <OverviewSection
        v-if="section === 'overview'"
        :stats="stats"
        :recent-jobs="recentJobs"
        :loading="summaryLoading"
        :types="availableTypes"
        @open-history="openHistory"
        @open-create="(type) => openCreate(type)"
        @launch-agent="openAgentLaunch"
        @open-approvals="openPendingApprovals"
        @open-detail="openDetail"
      />

      <HistorySection
        v-else-if="section === 'history'"
        v-model:job-type="filters.jobType"
        v-model:status="filters.status"
        v-model:page="page"
        :jobs="list"
        :total="listTotal"
        :loading="listLoading"
        :page-size="pageSize"
        :types="allJobTypes"
        @refresh="loadList"
        @open-detail="openDetail"
      />

      <SchedulesSection
        v-else
        :devices="devices"
        :devices-loading="devicesLoading"
        :ensure-devices="() => void loadDevices()"
      />
    </div>

    <!-- 任务详情抽屉(概览/历史/创建成功共用;终态后刷新列表与统计) -->
    <JobDetailDrawer ref="detailRef" @job-finished="() => void refreshAll()" />

    <!-- Agent 诊断时间线抽屉:发起后的实时进度/审批/报告;run 终态同步
         刷新任务列表(实时诊断已落任务记录,2026-10-30 统一入口) -->
    <TimelineDrawer ref="runDrawerRef" @finished="() => void refreshAll()" />

    <!-- 新建任务向导弹窗:发起统一在运维概览(类型卡/顶栏按钮/旧 URL 入口都打开它) -->
    <CreateWizard
      ref="wizardRef"
      :devices="devices"
      :devices-loading="devicesLoading"
      :types="availableTypes"
      @job-created="onJobCreated"
      @schedule-created="section = 'schedules'"
    />

    <!-- Agent 分析诊断发起弹窗:与其它任务类型同款交互(2026-10-27 起不占独立页签) -->
    <AgentLaunchDialog ref="launchRef" @started="onAgentStarted" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Clock, MagicStick, Plus, Timer } from '@element-plus/icons-vue'
import { agentAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useAutomationJobs } from '@/composables/useAutomationJobs'
import { useAutomationDevices } from '@/composables/useAutomationDevices'
import OverviewSection from './OverviewSection.vue'
import CreateWizard from './CreateWizard.vue'
import HistorySection from './HistorySection.vue'
import SchedulesSection from './SchedulesSection.vue'
import JobDetailDrawer from './JobDetailDrawer.vue'
import TimelineDrawer from './TimelineDrawer.vue'
import AgentLaunchDialog from './AgentLaunchDialog.vue'
import { availableJobTypesFor, allJobTypes } from './jobTypes'
import type { AutomationJobType } from '@/types/automation'

/**
 * 自动化运维壳:持分区导航与跨分区共享的数据(任务列表/统计/设备候选/
 * 最近诊断),各分区、抽屉与弹窗见同目录组件。发起入口全部在概览:
 * 任务类型卡开向导弹窗,智能诊断卡开发起弹窗,时间线/审批走抽屉。
 */
const props = withDefaults(defineProps<{ tab?: string }>(), { tab: '' })
const authStore = useAuthStore()
const availableTypes = computed(() => availableJobTypesFor((perm) => authStore.hasPermission(perm)))

type SectionKey = 'overview' | 'history' | 'schedules'
const sections = [
  { key: 'overview', label: '运维概览', icon: MagicStick },
  { key: 'history', label: '执行记录', icon: Clock },
  { key: 'schedules', label: '定时计划', icon: Timer },
] as const
const section = ref<SectionKey>('overview')

const {
  list,
  listTotal,
  listLoading,
  filters,
  page,
  pageSize,
  loadList,
  summaryLoading,
  recentJobs,
  stats,
  loadSummary,
  refreshAll,
  schedulePoll,
  stopPoll,
} = useAutomationJobs(section)
const { devices, loading: devicesLoading, load: loadDevices } = useAutomationDevices()

/** 待审批入口(铃铛/概览警告卡/旧 URL):直开最新一条等审批 run 的时间线;
    实时诊断已落任务记录(2026-10-30),此处不再维护独立的 run 列表快照 */
async function openPendingApprovals() {
  section.value = 'overview'
  try {
    const res = await agentAPI.listRuns({ page: 1, page_size: 20 })
    const pending = res.data.items.find((item) => item.waiting_approval)
    if (pending) void runDrawerRef.value?.open(pending.id)
  } catch {
    /* 拉不到就停在概览,不打扰 */
  }
}

const detailRef = ref<InstanceType<typeof JobDetailDrawer>>()
const runDrawerRef = ref<InstanceType<typeof TimelineDrawer>>()
const wizardRef = ref<InstanceType<typeof CreateWizard>>()
const launchRef = ref<InstanceType<typeof AgentLaunchDialog>>()

/** 发起统一在概览:切回概览页并打开向导弹窗(带类型则跳过选类型一步) */
function openCreate(jobType?: AutomationJobType) {
  section.value = 'overview'
  void loadDevices()
  wizardRef.value?.open(jobType)
}

function openAgentLaunch() {
  section.value = 'overview'
  launchRef.value?.open()
}

function onAgentStarted(runId: number) {
  void refreshAll() // 任务记录已随诊断创建,概览立刻出现"执行中"行
  void runDrawerRef.value?.open(runId)
}

function openHistory(status = '') {
  filters.status = status
  page.value = 1
  section.value = 'history'
  void loadList()
}
function openDetail(id: number) {
  void detailRef.value?.open(id)
}
function onJobCreated(jobId: number) {
  // 留在概览的发起上下文里,详情抽屉直接展示执行进度;最近任务卡随 refreshAll 更新
  void refreshAll()
  openDetail(jobId)
}

onMounted(() => {
  void loadSummary()
  void loadDevices()
})

watch(
  () => props.tab,
  (tab) => {
    const map: Record<string, AutomationJobType> = { scripts: 'script', inspection: 'inspection' }
    if (map[tab]) openCreate(map[tab])
    // 旧 URL ?tab=agent / 铃铛入口:直开待审批 run(智能诊断不再有独立页签)
    else if (tab === 'agent' || tab === 'agent-live') void openPendingApprovals()
    else if (!tab || tab === 'overview') section.value = 'overview'
  },
  { immediate: true },
)
watch(section, (s) => {
  if (s === 'history') void loadList() // 直接切 tab 也要拉列表(不只经概览入口进)
  if (s !== 'history' && s !== 'overview') stopPoll()
  else schedulePoll()
})

onUnmounted(stopPoll)
</script>

<style scoped>
.automation-center {
  background: var(--dcn-bg-page);
}
.automation-header {
  justify-content: space-between;
}
.automation-header > div {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.automation-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0 var(--dcn-space-6) var(--dcn-space-6);
}
.automation-tabs {
  position: sticky;
  top: 0;
  z-index: 3;
  display: flex;
  gap: 6px;
  padding: 12px 0;
  background: var(--dcn-bg-page);
  border-bottom: 1px solid var(--dcn-border-light);
}
.automation-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 40px;
  padding: 0 16px;
  border: 0;
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-secondary);
  background: transparent;
  cursor: pointer;
  font: inherit;
}
.automation-tabs button:hover {
  color: var(--dcn-text-primary);
  background: var(--dcn-bg-hover);
}
.automation-tabs button.active {
  color: var(--dcn-primary);
  background: var(--dcn-primary-soft);
  font-weight: 600;
}
:deep(.automation-page) {
  padding-top: var(--dcn-space-5);
}
@media (max-width: 760px) {
  .automation-body {
    padding-inline: 12px;
  }
}
</style>
