<template>
  <!-- 内容等 @opened(滑入动画结束)后再挂载:整段步骤+报告的 DOM 构建
       发生在动画帧里正是打开卡顿的主因之一;去掉 destroy-on-close 让
       二次打开复用已挂载的内容 -->
  <el-drawer v-model="visible" title="Agent 诊断时间线" size="60%" @opened="contentReady = true" @closed="stop">
    <div v-loading="loading || !contentReady" class="timeline-drawer-body">
      <template v-if="contentReady">
        <el-empty v-if="!loading && !run" description="诊断记录不存在或已被清理" :image-size="72" />
        <template v-else-if="run">
          <AgentRunTimeline :run="run" @updated="refresh" />
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { agentAPI } from '@/api'
import { usePolling } from '@/composables/usePolling'
import AgentRunTimeline from '../AgentRunTimeline.vue'
import type { AgentRunDetail } from '@/types/agent'

/**
 * Agent 诊断时间线抽屉:runId 驱动,open(runId) 打开;
 * 运行中 3s 轮询,决策后由 AgentRunTimeline 的 updated 事件触发单次刷新;
 * 终态时 emit('finished') 让父壳刷新最近诊断列表(每次 open 只发一次)。
 */
const emit = defineEmits<{ (e: 'finished'): void }>()

const visible = ref(false)
const loading = ref(false)
const run = ref<AgentRunDetail | null>(null)
/** 防竞态:连续打开多个 run 时丢弃迟到响应 */
let currentId: number | null = null
let finishedEmitted = false
/** 内容挂载闸:滑入动画结束后(@opened)才渲染时间线,动画帧里不建大 DOM;
 *  兜底定时器防 @opened 不触发(渲染被节流的后台/被遮挡面板里过渡事件
 *  可能不派发,内容会永远不渲染) */
const contentReady = ref(false)
let readyTimer: number | undefined
function armContentReady() {
  if (readyTimer != null) window.clearTimeout(readyTimer)
  readyTimer = window.setTimeout(() => (contentReady.value = true), 700)
}

const poll = usePolling(
  async () => {
    if (currentId == null) return
    try {
      const cur = await agentAPI.getRun(currentId)
      run.value = cur.data
      if (cur.data.status !== 'running') {
        poll.stop()
        if (!finishedEmitted) {
          finishedEmitted = true
          emit('finished')
        }
      }
    } catch {
      /* 网络抖动继续下一轮 */
    }
  },
  { intervalMs: 3000 },
)

async function open(runId: number) {
  poll.stop()
  currentId = runId
  finishedEmitted = false
  contentReady.value = false
  armContentReady()
  visible.value = true
  loading.value = true
  run.value = null
  try {
    const res = await agentAPI.getRun(runId)
    if (currentId !== runId) return
    run.value = res.data
    if (res.data.status === 'running') poll.start()
  } catch {
    /* 记录不存在/被清理:抽屉里显示空态 */
  } finally {
    loading.value = false
  }
}

async function refresh() {
  if (!run.value) return
  try {
    run.value = (await agentAPI.getRun(run.value.id)).data
  } catch {
    /* 决策回调后的刷新失败不打扰 */
  }
}

function stop() {
  poll.stop()
  currentId = null
  if (readyTimer != null) {
    window.clearTimeout(readyTimer)
    readyTimer = undefined
  }
}

defineExpose({ open })
</script>

<style scoped>
.timeline-drawer-body {
  min-height: 200px;
}
</style>
