<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useBigScreenPoller } from '@/composables/useBigScreenPoller'
import { useFullscreen } from '@/composables/useFullscreen'
import BigScreenHeader from '@/components/bigscreen/BigScreenHeader.vue'
import OverviewStats from '@/components/bigscreen/OverviewStats.vue'
import DeviceTypeChart from '@/components/bigscreen/DeviceTypeChart.vue'
import OnlineRateGauge from '@/components/bigscreen/OnlineRateGauge.vue'
import RoomSummary from '@/components/bigscreen/RoomSummary.vue'

const { overview, distribution, rooms, lastUpdated, start, stop } = useBigScreenPoller()

useFullscreen()

onMounted(() => {
  start()
})

onUnmounted(() => {
  stop()
})
</script>

<template>
  <div class="big-screen" data-theme="dark">
    <BigScreenHeader :last-updated="lastUpdated" />
    <div class="big-screen__body">
      <div class="big-screen__left">
        <OverviewStats :overview="overview" />
        <DeviceTypeChart :distribution="distribution" />
        <OnlineRateGauge :device-online="overview.device_online" :device-total="overview.device_total" />
      </div>
      <div class="big-screen__center">
        <RoomSummary :rooms="rooms" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.big-screen {
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
  font-family: var(--dcn-font-sans);
}

.big-screen__body {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3);
  overflow: hidden;
  min-height: 0;
}

.big-screen__left,
.big-screen__center {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
  min-height: 0;
  overflow: hidden;
}

.big-screen__left > *,
.big-screen__center > * {
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-xl);
  box-shadow: var(--dcn-shadow-sm);
}

.big-screen__center > :first-child {
  flex: 1;
}

/* Global panel title style */
:deep(.panel-title) {
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: var(--dcn-text-primary);
  padding-left: var(--dcn-space-3);
  border-left: 3px solid var(--dcn-primary);
  letter-spacing: 0.4px;
}

/* Scrollbar styling */
* {
  scrollbar-width: thin;
  scrollbar-color: var(--dcn-scrollbar-thumb) transparent;
}
</style>
