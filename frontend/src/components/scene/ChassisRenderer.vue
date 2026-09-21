<template>
  <div
    class="chassis"
    :class="['chassis--' + deviceType, { 'chassis--offline': status !== 'online' }]"
    @click="$emit('click', device)"
    @contextmenu.prevent="$emit('contextmenu', $event, device)"
  >
    <!-- Type color bar -->
    <div class="chassis-bar" :style="{ background: typeColor }" />

    <!-- Chassis glyph (HTML/CSS) -->
    <div class="chassis-glyph">
      <!-- Server: stacked drive sleds -->
      <div v-if="deviceType === 'server' || deviceType === 'cloud_server'" class="glyph-server">
        <div v-for="i in sledCount" :key="i" class="sled">
          <span class="sled-led" :class="i % 2 === 0 ? 'led-blue' : 'led-green'" />
          <span class="sled-line" />
        </div>
      </div>

      <!-- Host: monitor + stand -->
      <div v-else class="glyph-host">
        <div class="monitor">
          <div class="screen" />
        </div>
        <div class="stand" />
      </div>
    </div>

    <!-- Device info -->
    <div class="chassis-info" :class="{ 'chassis-info--compact': isCompact }">
      <div class="chassis-name">
        <span class="name-text" :title="device.name">{{ device.name }}</span>
        <span class="type-tag" :style="{ background: typeBg, color: typeColor }">
          {{ typeLabel }}
        </span>
      </div>
      <div class="chassis-meta">
        <span class="ip-text">{{ device.ip_address || '无 IP' }}</span>
        <span class="device-role">{{ roleLabel }}</span>
      </div>
    </div>

    <!-- Status dot -->
    <div class="chassis-status">
      <span class="status-dot" :class="'status-' + (status || 'offline')" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Device } from '@/types'
import { deviceTypeLabel, deviceTypeRole } from '@/utils/deviceLabels'

const props = defineProps<{
  device: Device
  heightU: number
  status?: string
}>()

defineEmits<{
  click: [device: Device]
  contextmenu: [event: MouseEvent, device: Device]
}>()

const deviceType = computed(() => props.device.type || 'host')

const typeColor = computed(() => `var(--dcn-device-${deviceType.value.replace(/_/g, '-')})`)
const typeBg = computed(() => `var(--dcn-device-${deviceType.value.replace(/_/g, '-')}-bg)`)

const typeLabel = computed(() => deviceTypeLabel(deviceType.value))
const roleLabel = computed(() => deviceTypeRole(deviceType.value))

// ── Glyph specifics ──

const sledCount = computed(() => Math.min(4, Math.max(2, Math.ceil(props.heightU * 1.5))))

// 1U devices use compact single-line layout
const isCompact = computed(() => props.heightU <= 1)
</script>

<style scoped>
.chassis {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 6px 0 0;
  background: linear-gradient(180deg, var(--dcn-bg-card) 0%, var(--dcn-bg-muted) 100%);
  border-block: 1px solid var(--dcn-border);
  border-left: none; /* bar handles left edge */
  cursor: pointer;
  user-select: none;
  transition: filter 0.15s;
  position: relative;
  overflow: hidden;
  width: 100%;
  height: 100%;
  min-width: 0;
  box-sizing: border-box;
}
.chassis:hover {
  filter: brightness(1.12);
  box-shadow: inset 0 0 0 1px var(--dcn-border-strong);
}
.chassis--offline {
  opacity: 0.75;
}
.chassis--server {
  background: linear-gradient(90deg, var(--dcn-device-server-bg), var(--dcn-bg-card) 34%);
}
.chassis--cloud_server {
  background: linear-gradient(90deg, var(--dcn-device-cloud-server-bg), var(--dcn-bg-card) 34%);
}
.chassis--host {
  background: linear-gradient(90deg, var(--dcn-device-host-bg), var(--dcn-bg-card) 34%);
}

.chassis-bar {
  width: 5px;
  align-self: stretch;
  margin: 1px 0;
  border-radius: 0 2px 2px 0;
  flex-shrink: 0;
}

/* ── Glyph container ── */
.chassis-glyph {
  width: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  height: 100%;
}

/* ── Server glyph ── */
.glyph-server {
  display: flex;
  flex-direction: column;
  gap: 1px;
  width: 56px;
  padding: 1px 0;
}
.sled {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 8px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 1px;
  padding: 0 2px;
}
.sled-led {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sled-line {
  flex: 1;
  height: 1px;
  background: var(--dcn-border-strong);
}
.led-green {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 2px rgba(16, 185, 129, 0.6);
}
.led-blue {
  background: #60a5fa;
  box-shadow: 0 0 2px rgba(96, 165, 250, 0.6);
}

/* ── Host glyph ── */
.glyph-host {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  width: 32px;
}
.monitor {
  width: 34px;
  height: 22px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1px;
}
.screen {
  width: 100%;
  height: 100%;
  background: linear-gradient(180deg, var(--dcn-device-host) 0%, transparent 100%);
  border-radius: 1px;
  opacity: 0.4;
}
.stand {
  width: 14px;
  height: 3px;
  background: var(--dcn-text-secondary);
  border-radius: 0 0 2px 2px;
}

/* ── Info section ── */
.chassis-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 0;
  overflow: hidden;
}
/* Compact mode for 1U devices: single-line layout */
.chassis-info--compact {
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
.chassis-info--compact .chassis-name {
  flex: 0 1 auto;
  min-width: 0;
}
.chassis-info--compact .chassis-meta {
  flex: 1;
  min-width: 0;
}
.chassis-name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name-text {
  font-size: 12px;
  font-weight: 600;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  flex-shrink: 1;
}
.type-tag {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 2px;
  font-weight: 500;
  flex-shrink: 0;
  line-height: 14px;
}
.chassis-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 10px;
  color: var(--dcn-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ip-text {
  font-family: var(--dcn-font-mono);
}
.device-role {
  color: var(--dcn-text-placeholder);
}
/* ── Status dot ── */
.chassis-status {
  width: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.status-online {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 4px rgba(16, 185, 129, 0.5);
}
.status-offline {
  background: var(--dcn-dot-offline);
}
</style>
