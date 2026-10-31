<template>
  <span class="countdown">{{ text }}</span>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import type { AgentActionApproval } from '@/types/agent'

/**
 * 审批倒计时(独立小组件持有 1s tick):数据轮询 3s 一跳,分秒必争的场景
 * 里数字要本地走秒;拆出来是为了让每秒的 tick 只重渲染这一小段,而不是
 * 整条时间线(步骤/报告一起重 diff,是抽屉卡顿的帮凶之一)。
 */
const props = defineProps<{ approvals: AgentActionApproval[]; approvalId: number }>()

const now = ref(Date.now())
const timer = window.setInterval(() => (now.value = Date.now()), 1000)
onUnmounted(() => window.clearInterval(timer))

const text = computed(() => {
  const approval = props.approvals.find((x) => x.id === props.approvalId)
  if (!approval?.expires_at) return ''
  const left = Math.max(0, new Date(approval.expires_at).getTime() - now.value)
  const m = Math.floor(left / 60000)
  const s = Math.floor((left % 60000) / 1000)
  return `${m}:${String(s).padStart(2, '0')} 后默认拒绝`
})
</script>

<style scoped>
.countdown {
  color: var(--dcn-warning-text);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
</style>
