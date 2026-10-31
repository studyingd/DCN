import { computed, reactive, ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { automationAPI } from '@/api'
import type { AutomationJobListItem, AutomationJobType } from '@/types/automation'
import { usePolling } from './usePolling'

const ACTIVE_STATUSES = ['pending', 'running']
/** 执行记录页每页条数 */
export const JOB_PAGE_SIZE = 20
/** 概览统计快照条数:统计卡数字的真实上限(更早的存量不计入) */
const SUMMARY_PAGE_SIZE = 100

/**
 * 自动化任务的两份独立列表状态:
 * - list:执行记录页(带筛选/分页,每页 20);
 * - summary:概览页统计卡 + 最近任务(无筛选快照,前 100 条)。
 * 旧实现概览统计直接取自(可能带历史页筛选的)列表第一页,设过筛选再切回
 * 概览且无在跑任务时数字会一直失真——拆成独立快照后概览口径恒定。
 *
 * 轮询:停留在概览/执行记录且对应集合里有 pending/running 任务时,每 5s
 * 静默刷新该集合(进度条随 steps_executed 爬升);离开两页或无在跑任务停表。
 */
export function useAutomationJobs(section: Ref<string>) {
  const list = ref<AutomationJobListItem[]>([])
  const listTotal = ref(0)
  const listLoading = ref(false)
  const filters = reactive<{ jobType: AutomationJobType | ''; status: string }>({ jobType: '', status: '' })
  const page = ref(1)

  const summary = ref<AutomationJobListItem[]>([])
  const summaryLoading = ref(false)

  const recentJobs = computed(() => summary.value.slice(0, 6))
  const stats = computed(() => ({
    running: summary.value.filter((i) => ACTIVE_STATUSES.includes(i.status)).length,
    completed: summary.value.filter((i) => i.status === 'completed').length,
    partial: summary.value.filter((i) => i.status === 'partial').length,
    failed: summary.value.filter((i) => i.status === 'failed').length,
  }))

  async function fetchList(silent: boolean) {
    if (!silent) listLoading.value = true
    try {
      const res = await automationAPI.jobs({
        job_type: filters.jobType || undefined,
        status: filters.status || undefined,
        page: page.value,
        page_size: JOB_PAGE_SIZE,
      })
      list.value = res.data.items
      listTotal.value = res.data.total
    } catch {
      // 静默轮询失败不打扰用户,下一轮再试
      if (!silent) ElMessage.error('加载任务记录失败')
    } finally {
      if (!silent) listLoading.value = false
    }
    schedulePoll()
  }
  async function fetchSummary(silent: boolean) {
    if (!silent) summaryLoading.value = true
    try {
      const res = await automationAPI.jobs({ page: 1, page_size: SUMMARY_PAGE_SIZE })
      summary.value = res.data.items
    } catch {
      if (!silent) ElMessage.error('加载任务统计失败')
    } finally {
      if (!silent) summaryLoading.value = false
    }
    schedulePoll()
  }

  const loadList = () => fetchList(false)
  const loadSummary = () => fetchSummary(false)
  /** 详情抽屉终态/创建成功后的静默兜底刷新(两个集合都可能变化) */
  const refreshAll = () => Promise.all([fetchList(true), fetchSummary(true)])

  function shouldPoll(): boolean {
    if (section.value !== 'history' && section.value !== 'overview') return false
    const source = section.value === 'history' ? list.value : summary.value
    return source.some((j) => ACTIVE_STATUSES.includes(j.status))
  }
  const poll = usePolling(
    async () => {
      await (section.value === 'history' ? fetchList(true) : fetchSummary(true))
    },
    { intervalMs: 5000, shouldContinue: shouldPoll },
  )

  /** 每次加载后重判:满足条件(重)启轮询链,否则停表 */
  function schedulePoll() {
    if (shouldPoll()) poll.start()
    else poll.stop()
  }

  return {
    list,
    listTotal,
    listLoading,
    filters,
    page,
    pageSize: JOB_PAGE_SIZE,
    loadList,
    summary,
    summaryLoading,
    recentJobs,
    stats,
    loadSummary,
    refreshAll,
    schedulePoll,
    stopPoll: poll.stop,
  }
}
