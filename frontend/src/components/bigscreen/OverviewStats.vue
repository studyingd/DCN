<script setup lang="ts">
import { computed } from 'vue'
import type { DashboardOverview } from '@/types/dashboard'

const props = defineProps<{
  overview: DashboardOverview
}>()

const cards = computed(() => {
  const o = props.overview
  const total = Math.max(o.device_total, 1)
  return [
    {
      label: '设备总数',
      value: o.device_total,
      color: '#5b9eff',
      bgColor: 'rgba(91, 158, 255, 0.15)',
      percent: 100,
      icon: `<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="#5b9eff" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`,
    },
    {
      label: '在线设备',
      value: o.device_online,
      color: '#00ff88',
      bgColor: 'rgba(0, 255, 136, 0.15)',
      percent: (o.device_online / total) * 100,
      icon: `<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="#00ff88" stroke-width="1.5"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
    },
    {
      label: '离线设备',
      value: o.device_offline,
      color: '#ff4d6a',
      bgColor: 'rgba(255, 77, 106, 0.15)',
      percent: (o.device_offline / total) * 100,
      icon: `<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="#ff4d6a" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    },
    {
      label: '维护中',
      value: o.device_maintenance,
      color: '#ff9500',
      bgColor: 'rgba(255, 149, 0, 0.15)',
      percent: (o.device_maintenance / total) * 100,
      icon: `<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="#ff9500" stroke-width="1.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    },
  ]
})
</script>

<template>
  <div class="overview-stats">
    <div class="panel-title">设备概览</div>
    <div class="overview-stats__grid">
      <div
        v-for="card in cards"
        :key="card.label"
        class="overview-stats__card"
        :style="{ background: card.bgColor }"
      >
        <div class="overview-stats__icon" v-html="card.icon" />
        <div class="overview-stats__info">
          <div class="overview-stats__value" :style="{ color: card.color }">
            {{ card.value }}
          </div>
          <div class="overview-stats__label">{{ card.label }}</div>
        </div>
        <div class="overview-stats__bar">
          <div
            class="overview-stats__bar-fill"
            :style="{
              width: card.percent + '%',
              background: `linear-gradient(90deg, ${card.color}, ${card.color}88)`,
            }"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overview-stats {
  padding: var(--dcn-space-3);
}

.overview-stats__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-3);
}

.overview-stats__card {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3);
  border-radius: var(--dcn-radius-xl);
  border: 1px solid rgba(91, 158, 255, 0.15);
  transition: transform 0.3s ease;
}

.overview-stats__card:hover {
  transform: translateY(-2px);
}

.overview-stats__icon {
  flex-shrink: 0;
}

.overview-stats__info {
  flex: 1;
  min-width: 0;
}

.overview-stats__value {
  font-size: var(--dcn-text-3xl);
  font-weight: 700;
  font-family: var(--dcn-font-mono);
  line-height: 1;
  transition: all 0.6s ease;
}

.overview-stats__label {
  font-size: var(--dcn-text-sm);
  color: rgba(200, 215, 245, 0.65);
  margin-top: var(--dcn-space-1);
}

.overview-stats__bar {
  width: 100%;
  height: 3px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: var(--dcn-radius-xs);
  overflow: hidden;
}

.overview-stats__bar-fill {
  height: 100%;
  border-radius: var(--dcn-radius-xs);
  transition: width 0.8s ease;
}
</style>
