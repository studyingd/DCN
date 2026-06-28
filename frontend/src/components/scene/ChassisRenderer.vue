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
      <div v-if="deviceType === 'server'" class="glyph-server">
        <div v-for="i in sledCount" :key="i" class="sled">
          <span class="sled-led" :class="i % 2 === 0 ? 'led-blue' : 'led-green'" />
          <span class="sled-line" />
        </div>
      </div>

      <!-- Switch: full-width port grid -->
      <div v-else-if="deviceType === 'switch'" class="glyph-switch">
        <div class="switch-body">
          <span
            v-for="i in glyphPortCount"
            :key="i"
            class="switch-port-slot"
          />
        </div>
      </div>

      <!-- Router: box + antennas -->
      <div v-else-if="deviceType === 'router'" class="glyph-router">
        <div class="router-antennas">
          <span v-for="i in 2" :key="i" class="antenna" />
        </div>
        <div class="router-body">
          <span class="router-led led-green" />
          <span class="router-led led-blue" />
        </div>
      </div>

      <!-- Firewall: shield shape -->
      <div v-else-if="deviceType === 'firewall'" class="glyph-firewall">
        <div class="shield">
          <span class="shield-led" />
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
        <span v-if="interfaceStatus && interfaceStatus.source === 'ssh'" class="port-summary">
          {{ interfaceStatus.up }}/{{ interfaceStatus.total }} 端口
        </span>
        <span v-else-if="interfaceStatus && interfaceStatus.source === 'unavailable' && interfaceStatus.error" class="port-error" :title="interfaceStatus.error">
          {{ interfaceStatus.error.slice(0, 20) }}
        </span>
      </div>
    </div>

    <!-- Port LED strip -->
    <div class="port-led-strip" :class="'port-led-strip--' + ledScale">
      <el-tooltip
        v-for="(p, i) in portDisplay"
        :key="i"
        :content="p.name ? `${p.name} ${p.status === 'up' ? '● 已连接' : '○ 未连接'}` : ''"
        :disabled="!p.name"
        placement="top"
        :show-after="100"
        effect="dark"
      >
        <span
          class="port-led"
          :class="'port-led--' + p.status"
        />
      </el-tooltip>
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
import type { InterfaceStatusEntry } from '@/composables/useInterfaceStatus'

const props = defineProps<{
  device: Device
  heightU: number
  interfaceStatus?: InterfaceStatusEntry
  status?: string
}>()

defineEmits<{
  click: [device: Device]
  contextmenu: [event: Event, device: Device]
}>()

const U_HEIGHT = 24  // used for sledCount / glyphPortCount / maxLeds calculations

const deviceType = computed(() => props.device.type || 'host')

const typeColor = computed(() => `var(--dcn-device-${deviceType.value})`)
const typeBg = computed(() => `var(--dcn-device-${deviceType.value}-bg)`)

const typeLabel = computed(() => {
  const m: Record<string, string> = {
    server: '服务器', switch: '交换机', router: '路由器',
    firewall: '防火墙', host: '主机',
  }
  return m[deviceType.value] || deviceType.value
})

// ── Glyph specifics ──

const sledCount = computed(() => Math.min(4, Math.max(2, Math.ceil(props.heightU * 1.5))))
const glyphPortCount = computed(() => {
  // When real interface data is available, use actual port count (clamped for visual)
  if (props.interfaceStatus && props.interfaceStatus.source === 'ssh' && props.interfaceStatus.total > 0) {
    return Math.min(48, props.interfaceStatus.total)
  }
  // Fallback: decorative estimate based on U height
  if (deviceType.value === 'switch') return Math.min(16, Math.max(8, props.heightU * 8))
  return 8
})

// ── Port LED display ──

const maxLeds = computed(() => {
  // When real interface data is available, show actual port count (up to 48 for visual sanity)
  if (props.interfaceStatus && props.interfaceStatus.source === 'ssh' && props.interfaceStatus.total > 0) {
    return Math.min(48, props.interfaceStatus.total)
  }
  // Fallback: per-type decorative estimate
  switch (deviceType.value) {
    case 'switch': return Math.min(16, Math.max(8, props.heightU * 8))
    case 'router': return 4
    case 'firewall': return 4
    case 'server': return 2
    case 'host': return 1
    default: return 2
  }
})

const ledScale = computed(() => maxLeds.value > 12 ? 'dense' : 'normal')

// 1U devices use compact single-line layout
const isCompact = computed(() => props.heightU <= 1)

const portDisplay = computed<Array<{ name: string; status: 'up' | 'down' | 'unknown' }>>(() => {
  const ifs = props.interfaceStatus
  // If SSH succeeded, show real statuses
  if (ifs && ifs.source === 'ssh' && ifs.interfaces.length > 0) {
    return ifs.interfaces.slice(0, maxLeds.value).map(p => ({
      name: p.name,
      status: (p.status === 'up' ? 'up' : 'down') as 'up' | 'down',
    }))
  }
  // Else: skeleton placeholders (gray)
  return Array.from({ length: maxLeds.value }, () => ({
    name: '',
    status: 'unknown' as const,
  }))
})
</script>

<style scoped>
.chassis {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 6px 0 0;
  background: linear-gradient(180deg, var(--dcn-bg-card) 0%, var(--dcn-bg-muted) 100%);
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
.chassis:hover { filter: brightness(1.15); }
.chassis--offline { opacity: 0.75; }

.chassis-bar {
  width: 5px;
  align-self: stretch;
  margin: 1px 0;
  border-radius: 0 2px 2px 0;
  flex-shrink: 0;
}

/* ── Glyph container ── */
.chassis-glyph {
  width: 44px;
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
  width: 40px;
  padding: 1px 0;
}
.sled {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 5px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 1px;
  padding: 0 2px;
}
.sled-led { width: 3px; height: 3px; border-radius: 50%; flex-shrink: 0; }
.sled-line { flex: 1; height: 1px; background: var(--dcn-border-strong); }
.led-green { background: var(--dcn-dot-online); box-shadow: 0 0 2px rgba(16,185,129,0.6); }
.led-blue { background: #60a5fa; box-shadow: 0 0 2px rgba(96,165,250,0.6); }

/* ── Switch glyph ── */
.glyph-switch {
  width: 42px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.switch-body {
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 1px;
  padding: 2px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 2px;
  width: 100%;
}
.switch-port-slot {
  width: 100%;
  height: 3px;
  background: var(--dcn-border-strong);
  border-radius: 1px;
  min-height: 3px;
}

/* ── Router glyph ── */
.glyph-router {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  width: 36px;
}
.router-antennas { display: flex; gap: 4px; height: 5px; align-items: flex-end; }
.antenna { width: 1px; height: 5px; background: var(--dcn-text-secondary); }
.router-body {
  display: flex;
  gap: 3px;
  align-items: center;
  justify-content: center;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 2px;
  padding: 2px 4px;
  width: 100%;
  height: 8px;
}
.router-led { width: 3px; height: 3px; border-radius: 50%; }

/* ── Firewall glyph ── */
.glyph-firewall {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
}
.shield {
  width: 18px;
  height: 18px;
  background: var(--dcn-bg-section);
  border: 1.5px solid var(--dcn-device-firewall);
  border-radius: 50% 50% 50% 50% / 30% 30% 70% 70%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}
.shield-led {
  width: 4px; height: 4px;
  background: var(--dcn-device-firewall);
  border-radius: 50%;
  box-shadow: 0 0 3px rgba(251, 191, 36, 0.6);
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
  width: 24px;
  height: 12px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1px;
}
.screen {
  width: 100%; height: 100%;
  background: linear-gradient(180deg, var(--dcn-device-host) 0%, transparent 100%);
  border-radius: 1px;
  opacity: 0.4;
}
.stand { width: 8px; height: 2px; background: var(--dcn-text-secondary); border-radius: 0 0 1px 1px; }

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
.ip-text { font-family: var(--dcn-font-mono); }
.port-summary { color: var(--dcn-text-regular); }
.port-error { color: var(--el-color-danger); font-style: italic; }

/* ── Port LED strip ── */
.port-led-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  align-content: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 2px;
  background: var(--dcn-bg-section);
  border-radius: 3px;
  border: 1px solid var(--dcn-border);
  width: 92px;
  align-self: center;
}
.port-led-strip--dense {
  width: 110px;
}
.port-led {
  width: 4px;
  height: 4px;
  border-radius: 1px;
  transition: background 0.3s, box-shadow 0.3s, transform 0.15s;
  flex-shrink: 0;
  cursor: default;
  display: inline-block;
  /* Expand hit area without changing visual size */
  position: relative;
}
.port-led::before {
  content: '';
  position: absolute;
  inset: -4px;
}
.port-led--up {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 3px rgba(16,185,129,0.6);
}
.port-led--up:hover {
  box-shadow: 0 0 6px rgba(16,185,129,0.9);
  transform: scale(1.6);
}
.port-led--down {
  background: var(--dcn-dot-offline);
}
.port-led--down:hover {
  box-shadow: 0 0 4px rgba(239,68,68,0.5);
  transform: scale(1.6);
}
.port-led--unknown {
  background: var(--dcn-border-strong);
  cursor: auto;
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
.status-online { background: var(--dcn-dot-online); box-shadow: 0 0 4px rgba(16,185,129,0.5); }
.status-offline { background: var(--dcn-dot-offline); }
.status-maintenance { background: var(--dcn-dot-maintenance); }
</style>
