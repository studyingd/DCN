<script setup lang="ts">
import { computed } from 'vue'
import type { AuditEventItem } from '@/types/dashboard'

const props = defineProps<{
  events: AuditEventItem[]
}>()

const SCRIPT_TYPES = ['script_executed', 'power_shutdown', 'power_reboot']

const scriptEvents = computed(() =>
  props.events
    .filter((e) => SCRIPT_TYPES.includes(e.event_type))
    .slice(0, 5)
)

const totalScripts = computed(() =>
  props.events.filter((e) => SCRIPT_TYPES.includes(e.event_type)).length
)

function formatTime(dateStr: string): string {
  const d = new Date(dateStr)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
</script>

<template>
  <div class="script-stats">
    <div class="panel-title">脚本执行</div>

    <div class="script-stats__summary">
      <div class="script-stats__stat">
        <span class="script-stats__stat-value">{{ totalScripts }}</span>
        <span class="script-stats__stat-label">今日执行</span>
      </div>
    </div>

    <div class="script-stats__list">
      <div
        v-for="event in scriptEvents"
        :key="event.id"
        class="script-stats__item"
      >
        <div class="script-stats__item-header">
          <span class="script-stats__item-user">{{ event.username || '-' }}</span>
          <span class="script-stats__item-time">{{ formatTime(event.created_at) }}</span>
        </div>
        <div class="script-stats__item-command" :title="event.command || ''">
          {{ event.command || '-' }}
        </div>
        <div v-if="event.device_name" class="script-stats__item-device">
          目标: {{ event.device_name }}
        </div>
      </div>
      <div v-if="scriptEvents.length === 0" class="script-stats__empty">
        暂无脚本执行记录
      </div>
    </div>
  </div>
</template>

<style scoped>
.script-stats {
  padding: var(--dcn-space-3);
  display: flex;
  flex-direction: column;
}

.script-stats__summary {
  display: flex;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-3);
}

.script-stats__stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--dcn-space-2) var(--dcn-space-4);
  background: rgba(255, 149, 0, 0.1);
  border: 1px solid rgba(255, 149, 0, 0.2);
  border-radius: var(--dcn-radius-lg);
  flex: 1;
}

.script-stats__stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #ff9500;
  font-family: var(--dcn-font-mono);
}

.script-stats__stat-label {
  font-size: var(--dcn-text-xs);
  color: rgba(200, 215, 245, 0.5);
  margin-top: var(--dcn-radius-xs);
}

.script-stats__list {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
  overflow-y: auto;
  max-height: 150px;
  scrollbar-width: thin;
  scrollbar-color: rgba(0, 170, 255, 0.3) transparent;
}

.script-stats__list::-webkit-scrollbar {
  width: var(--dcn-space-1);
}

.script-stats__list::-webkit-scrollbar-thumb {
  background: rgba(0, 170, 255, 0.3);
  border-radius: var(--dcn-radius-xs);
}

.script-stats__item {
  padding: var(--dcn-space-2) var(--dcn-space-2);
  background: rgba(10, 14, 39, 0.4);
  border: 1px solid rgba(200, 215, 245, 0.06);
  border-radius: var(--dcn-radius-md);
}

.script-stats__item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--dcn-radius-xs);
}

.script-stats__item-user {
  font-size: var(--dcn-text-xs);
  color: #00ccff;
  font-weight: 500;
}

.script-stats__item-time {
  font-size: 10px;
  color: rgba(200, 215, 245, 0.35);
  font-family: var(--dcn-font-mono);
}

.script-stats__item-command {
  font-size: var(--dcn-text-xs);
  color: rgba(200, 215, 245, 0.7);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--dcn-font-mono);
}

.script-stats__item-device {
  font-size: 10px;
  color: rgba(200, 215, 245, 0.4);
  margin-top: var(--dcn-radius-xs);
}

.script-stats__empty {
  text-align: center;
  color: rgba(200, 215, 245, 0.35);
  padding: var(--dcn-space-4) 0;
  font-size: var(--dcn-text-base);
}
</style>
