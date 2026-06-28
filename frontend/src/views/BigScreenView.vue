<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useBigScreenPoller } from '@/composables/useBigScreenPoller'
import { useFullscreen } from '@/composables/useFullscreen'
import BigScreenHeader from '@/components/bigscreen/BigScreenHeader.vue'
import OverviewStats from '@/components/bigscreen/OverviewStats.vue'
import DeviceTypeChart from '@/components/bigscreen/DeviceTypeChart.vue'
import OnlineRateGauge from '@/components/bigscreen/OnlineRateGauge.vue'
import TopologyGraph from '@/components/bigscreen/TopologyGraph.vue'
import RoomSummary from '@/components/bigscreen/RoomSummary.vue'
import AuditTimeline from '@/components/bigscreen/AuditTimeline.vue'
import LoginTrendChart from '@/components/bigscreen/LoginTrendChart.vue'
import ScriptStats from '@/components/bigscreen/ScriptStats.vue'

const {
  overview,
  distribution,
  rooms,
  topologyNodes,
  topologyEdges,
  recentEvents,
  eventCountsToday,
  loginTrend,
  lastUpdated,
  start,
  stop,
} = useBigScreenPoller()

useFullscreen()

onMounted(() => {
  start()
})

onUnmounted(() => {
  stop()
})
</script>

<template>
  <div class="big-screen">
    <BigScreenHeader :last-updated="lastUpdated" />
    <div class="big-screen__body">
      <div class="big-screen__left">
        <OverviewStats :overview="overview" />
        <DeviceTypeChart :distribution="distribution" />
        <OnlineRateGauge :device-online="overview.device_online" :device-total="overview.device_total" />
      </div>
      <div class="big-screen__center">
        <TopologyGraph :nodes="topologyNodes" :edges="topologyEdges" />
        <RoomSummary :rooms="rooms" />
      </div>
      <div class="big-screen__right">
        <AuditTimeline :events="recentEvents" :counts="eventCountsToday" />
        <LoginTrendChart :trend="loginTrend" />
        <ScriptStats :events="recentEvents" />
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
  background: linear-gradient(135deg, #0a0e27 0%, #0d1135 50%, #0a0e27 100%);
  color: #e8edf5;
  font-family: var(--dcn-font-sans);
}

.big-screen__body {
  flex: 1;
  display: grid;
  grid-template-columns: 25% 50% 25%;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3);
  overflow: hidden;
  min-height: 0;
}

.big-screen__left,
.big-screen__center,
.big-screen__right {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
  min-height: 0;
  overflow: hidden;
}

.big-screen__left > *,
.big-screen__center > *,
.big-screen__right > * {
  background: rgba(13, 25, 65, 0.75);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(45, 90, 200, 0.25);
  border-radius: var(--dcn-radius-xl);
  box-shadow: 0 0 20px rgba(0, 100, 200, 0.1);
}

.big-screen__center > :first-child {
  flex: 1;
}

/* Global panel title style */
:deep(.panel-title) {
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: #e8edf5;
  padding-left: var(--dcn-space-3);
  border-left: 3px solid #00aaff;
  letter-spacing: 1px;
}

/* Scrollbar styling */
* {
  scrollbar-width: thin;
  scrollbar-color: rgba(0, 170, 255, 0.3) transparent;
}

::-webkit-scrollbar {
  width: var(--dcn-radius-xs);
  height: var(--dcn-radius-xs);
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: rgba(0, 170, 255, 0.3);
  border-radius: var(--dcn-radius-xs);
}
</style>
