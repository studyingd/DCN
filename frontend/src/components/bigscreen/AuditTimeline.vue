<script setup lang="ts">
import { ref, watch, nextTick, computed } from 'vue'
import type { AuditEventItem } from '@/types/dashboard'

const props = defineProps<{
  events: AuditEventItem[]
  counts: Record<string, number>
}>()

const EVENT_COLORS: Record<string, string> = {
  session_start: '#00ff88',
  session_end: '#888888',
  command_executed: '#5b9eff',
  command_blocked: '#ff4d6a',
  system_login: '#00ccff',
  script_executed: '#ff9500',
}

const EVENT_LABELS: Record<string, string> = {
  session_start: '会话开始',
  session_end: '会话结束',
  command_executed: '命令执行',
  command_blocked: '命令拦截',
  system_login: '系统登录',
  script_executed: '脚本执行',
}

const displayedEvents = computed(() => props.events.slice(0, 20))

const listRef = ref<HTMLElement | null>(null)

watch(
  () => props.events.length,
  () => {
    nextTick(() => {
      if (listRef.value) {
        listRef.value.scrollTop = listRef.value.scrollHeight
      }
    })
  }
)

function formatTime(dateStr: string): string {
  const d = new Date(dateStr)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
</script>

<template>
  <div class="audit-timeline">
    <div class="panel-title">审计事件</div>

    <div v-if="Object.keys(counts).length > 0" class="audit-timeline__badges">
      <span
        v-for="(count, type) in counts"
        :key="type"
        class="audit-timeline__badge"
        :style="{ borderColor: EVENT_COLORS[type] || '#5b9eff', color: EVENT_COLORS[type] || '#5b9eff' }"
      >
        {{ EVENT_LABELS[type] || type }}: {{ count }}
      </span>
    </div>

    <div ref="listRef" class="audit-timeline__list">
      <div
        v-for="event in displayedEvents"
        :key="event.id"
        class="audit-timeline__item"
      >
        <span
          class="audit-timeline__dot"
          :style="{ background: EVENT_COLORS[event.event_type] || '#5b9eff' }"
        />
        <span class="audit-timeline__time">{{ formatTime(event.created_at) }}</span>
        <span class="audit-timeline__user">{{ event.username || '-' }}</span>
        <span class="audit-timeline__action">
          {{ EVENT_LABELS[event.event_type] || event.event_type }}
          <template v-if="event.device_name">
            → {{ event.device_name }}
          </template>
        </span>
      </div>
      <div v-if="displayedEvents.length === 0" class="audit-timeline__empty">
        暂无审计事件
      </div>
    </div>
  </div>
</template>

<style scoped>
.audit-timeline {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.audit-timeline__badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-3);
}

.audit-timeline__badge {
  font-size: var(--dcn-text-xs);
  padding: var(--dcn-radius-xs) var(--dcn-space-2);
  border: 1px solid;
  border-radius: var(--dcn-radius-xl);
  background: rgba(10, 14, 39, 0.5);
}

.audit-timeline__list {
  flex: 1;
  overflow-y: auto;
  max-height: 220px;
  scrollbar-width: thin;
  scrollbar-color: rgba(0, 170, 255, 0.3) transparent;
}

.audit-timeline__list::-webkit-scrollbar {
  width: var(--dcn-space-1);
}

.audit-timeline__list::-webkit-scrollbar-thumb {
  background: rgba(0, 170, 255, 0.3);
  border-radius: var(--dcn-radius-xs);
}

.audit-timeline__item {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) 0;
  border-bottom: 1px solid rgba(200, 215, 245, 0.05);
  font-size: var(--dcn-text-sm);
  color: rgba(200, 215, 245, 0.75);
}

.audit-timeline__dot {
  flex-shrink: 0;
  width: var(--dcn-space-2);
  height: var(--dcn-space-2);
  border-radius: var(--dcn-radius-full);
}

.audit-timeline__time {
  color: rgba(200, 215, 245, 0.45);
  font-family: var(--dcn-font-mono);
  font-size: var(--dcn-text-xs);
  white-space: nowrap;
}

.audit-timeline__user {
  color: #00ccff;
  font-weight: 500;
  max-width: 60px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.audit-timeline__action {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.audit-timeline__empty {
  text-align: center;
  color: rgba(200, 215, 245, 0.35);
  padding: var(--dcn-space-5) 0;
  font-size: var(--dcn-text-base);
}
</style>
