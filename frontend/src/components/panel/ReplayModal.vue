<template>
  <Teleport to="body">
    <transition name="replay-modal-fade">
      <div v-if="showReplay" class="replay-modal-overlay" @click.self="close">
        <div class="replay-modal">
          <!-- Header bar -->
          <div class="replay-modal-header">
            <div class="replay-modal-title">
              <el-icon :size="18"><VideoPlay /></el-icon>
              <span>{{ replayDevice }}</span>
              <el-tag v-if="replayConnType === 'rdp'" size="small" type="warning">RDP</el-tag>
              <el-tag v-else size="small">SSH</el-tag>
            </div>
            <div class="replay-modal-header-right">
              <span class="replay-modal-elapsed">{{ formatReplayTime(replayConnType === 'rdp' ? rdpCurrentTime : replayCurrentTime) }} / {{ formatReplayTime(replayConnType === 'rdp' ? rdpDuration : replayDuration) }}</span>
              <el-button size="small" :icon="Close" text class="replay-modal-close-btn" @click="close" />
            </div>
          </div>

          <!-- Display area -->
          <div class="replay-modal-body">
            <div ref="replayContainerRef" class="replay-modal-display" :class="{ 'is-rdp': replayConnType === 'rdp' }" />
            <div class="replay-watermark" :style="replayWatermarkStyle" />
            <div v-if="replayLoadError" class="replay-modal-error">
              <el-icon :size="32" style="color: var(--dcn-danger);"><WarningFilled /></el-icon>
              <p>加载录像失败</p>
            </div>
          </div>

          <!-- Control bar -->
          <div class="replay-modal-controls">
            <el-button size="small" text class="ctrl-btn"
              :icon="replayIsPlaying ? VideoPause : VideoPlay"
              @click="toggleReplayPlayback"
            />
            <el-slider
              :model-value="replaySliderValue"
              @input="onReplaySliderInput"
              @change="onReplaySliderChange"
              :max="replayConnType === 'rdp' ? rdpDuration : replayDuration"
              :step="0.1"
              :show-tooltip="false"
              class="ctrl-slider"
            />
            <el-select
              :model-value="replayConnType === 'rdp' ? rdpPlaybackSpeed : replaySpeed"
              size="small"
              class="ctrl-speed"
              @change="replayConnType === 'rdp' ? rdpSetSpeed($event) : replaySpeed = $event"
            >
              <el-option :value="0.5" label="0.5x" />
              <el-option :value="1" label="1x" />
              <el-option :value="2" label="2x" />
              <el-option :value="4" label="4x" />
            </el-select>
          </div>
        </div>
      </div>
    </transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onUnmounted } from 'vue'
import { VideoPlay, VideoPause, Close, WarningFilled } from '@element-plus/icons-vue'
import { auditAPI } from '@/api'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { useRdpReplay } from '@/composables/useRdpReplay'

const emit = defineEmits<{
  (e: 'close'): void
}>()

// ---- RDP replay integration ----
const rdpReplay = useRdpReplay()
const {
  isPlaying: rdpIsPlaying,
  currentTime: rdpCurrentTime,
  duration: rdpDuration,
  playbackSpeed: rdpPlaybackSpeed,
  loadError: rdpLoadError,
  init: rdpInit,
  play: rdpPlay,
  pause: rdpPause,
  seek: rdpSeek,
  setSpeed: rdpSetSpeed,
  destroy: rdpDestroy,
} = rdpReplay

// ---- SSH replay state ----
const showReplay = ref(false)
const replayDevice = ref('')
const replayUsername = ref('')
const replayDate = ref('')
const replaySessionId = ref(0)
const replayContainerRef = ref<HTMLElement>()
const replayState = ref<'idle' | 'playing' | 'paused'>('idle')
const replayProgress = ref(0)
const replayCurrentTime = ref(0)
const replayDuration = ref(0)
const replaySpeed = ref(1)
const replayLoadError = ref(false)
const replayConnType = ref<'ssh' | 'rdp'>('ssh')

let replayTerm: Terminal | null = null
let replayFitAddon: FitAddon | null = null
let replayEvents: Array<[string, number, string]> = []
let replayStartTime = 0
let replayAnimFrame = 0
let replayNextIdx = 0

// ---- Watermark ----
const replayWatermarkStyle = computed(() => {
  const username = replayUsername.value
  if (!username) return {}
  // Sanitize against XML injection in SVG
  const safeName = username.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;')
  const text = replayDate.value ? `${safeName}  ${replayDate.value}` : safeName
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="260" height="100">
    <text x="130" y="50" text-anchor="middle" font-size="14" font-family="Arial,sans-serif" fill="rgba(255,255,255,0.13)" font-weight="bold">${text}</text>
  </svg>`
  const encoded = encodeURIComponent(svg)
  return { backgroundImage: `url("data:image/svg+xml,${encoded}")` }
})

// ---- Unified replay state helpers ----
const replayIsPlaying = computed(() => {
  if (replayConnType.value === 'rdp') return rdpIsPlaying.value
  return replayState.value === 'playing'
})

const replaySliderValue = computed({
  get: () => replayConnType.value === 'rdp' ? rdpCurrentTime.value : replayProgress.value,
  set: (val: number) => {
    if (replayConnType.value === 'rdp') {
      rdpSeek(val)
    } else {
      replaySeek(val)
    }
  },
})

let _seekTimer: ReturnType<typeof setTimeout> | null = null

function onReplaySliderInput(val: number) {
  if (replayConnType.value === 'rdp') {
    rdpCurrentTime.value = val
  } else {
    replayCurrentTime.value = val
  }
}

function onReplaySliderChange(val: number) {
  if (_seekTimer) clearTimeout(_seekTimer)
  if (replayConnType.value === 'rdp') {
    rdpSeek(val)
  } else {
    replaySeek(val)
  }
}

function toggleReplayPlayback() {
  if (replayConnType.value === 'rdp') {
    if (rdpIsPlaying.value) {
      rdpPause()
    } else {
      if (rdpCurrentTime.value >= rdpDuration.value && rdpDuration.value > 0) {
        rdpSeek(0)
      }
      rdpPlay()
    }
  } else {
    if (replayState.value === 'playing') {
      replayPause()
    } else {
      if (replayCurrentTime.value >= replayDuration.value && replayDuration.value > 0) {
        replaySeek(0)
      }
      replayPlay()
    }
  }
}

// ---- Open / close ----
async function open(row: any) {
  rdpDestroy()
  destroyReplay()
  replayDevice.value = row.device_name || 'Unknown'
  replayUsername.value = row.operator || row.username || ''
  if (row.started_at) {
    const d = new Date(row.started_at)
    replayDate.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } else {
    replayDate.value = ''
  }
  replaySessionId.value = row.id
  replayState.value = 'idle'
  replayProgress.value = 0
  replayCurrentTime.value = 0
  replayDuration.value = row.duration_seconds || 0
  replayLoadError.value = false
  replayConnType.value = row.conn_type === 'rdp' ? 'rdp' : 'ssh'
  showReplay.value = true
  await nextTick()
  if (replayConnType.value === 'rdp') {
    if (replayContainerRef.value) {
      rdpInit(row.id, replayContainerRef.value)
    }
  } else {
    initReplay()
  }
}

function close() {
  rdpDestroy()
  destroyReplay()
  showReplay.value = false
  emit('close')
}

// ---- SSH asciicast replay ----
async function initReplay() {
  if (!replayContainerRef.value) return

  // xterm.dispose() does NOT remove the DOM nodes it appended to the host
  // element. Without this, every open() accumulates one more stale xterm
  // instance inside the container, which is why replay sometimes showed
  // blank/overlapping terminals after a few uses. The container is freshly
  // mounted here (after showReplay=true + nextTick), so it should be empty
  // — but clear it defensively in case of HMR or rapid re-opens.
  replayContainerRef.value.innerHTML = ''

  try {
    const res = await auditAPI.getRecordingData(replaySessionId.value)
    const castData = (res.data as any).data

    // Parse asciicast v2 format
    const lines = castData.split('\n').filter(Boolean)
    replayEvents = []
    for (const line of lines) {
      try {
        const parsed = JSON.parse(line)
        if (Array.isArray(parsed) && parsed.length >= 3) {
          if (parsed[0] === 'o') {
            replayEvents.push([parsed[0], parsed[1], parsed[2]])
          }
        } else if (parsed.version === 2) {
          if (parsed.width && parsed.height) {
            replayTerm?.resize(parsed.width, parsed.height)
          }
        }
      } catch { /* skip malformed */ }
    }

    // Compute total duration
    if (replayEvents.length > 0) {
      const last = replayEvents[replayEvents.length - 1]
      replayDuration.value = Math.ceil(last[1])
    }

    // Create xterm.js instance
    replayTerm = new Terminal({
      cursorBlink: false,
      fontSize: 14,
      fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", Menlo, Monaco, "Courier New", monospace',
      theme: {
        background: '#1e1e1e',
        foreground: '#d4d4d4',
        cursor: '#d4d4d4',
        selectionBackground: '#264f78',
        black: '#000000', red: '#cd3131', green: '#0dbc79', yellow: '#e5e510',
        blue: '#2472c8', magenta: '#bc3fbc', cyan: '#11a8cd', white: '#e5e5e5',
        brightBlack: '#666666', brightRed: '#f14c4c', brightGreen: '#23d18b',
        brightYellow: '#f5f543', brightBlue: '#3b8eea', brightMagenta: '#d670d6',
        brightCyan: '#29b8db', brightWhite: '#ffffff',
      },
      scrollback: 10000,
      convertEol: false,
    })

    replayFitAddon = new FitAddon()
    replayTerm.loadAddon(replayFitAddon)
    replayTerm.open(replayContainerRef.value)
    await nextTick()
    replayFitAddon.fit()

  } catch {
    replayLoadError.value = true
  }
}

function replayPlay() {
  if (!replayTerm || replayEvents.length === 0) return
  if (replayState.value === 'playing') return

  replayState.value = 'playing'
  const wallStart = performance.now()
  const offset = replayCurrentTime.value
  replayNextIdx = replayEvents.findIndex(e => e[1] >= offset)
  if (replayNextIdx < 0) replayNextIdx = replayEvents.length

  function tick() {
    if (replayState.value !== 'playing') return

    const elapsed = (performance.now() - wallStart) / 1000 * replaySpeed.value + offset

    while (replayNextIdx < replayEvents.length && replayEvents[replayNextIdx][1] <= elapsed) {
      replayTerm!.write(replayEvents[replayNextIdx][2])
      replayNextIdx++
    }

    replayCurrentTime.value = elapsed
    replayProgress.value = elapsed

    if (replayNextIdx >= replayEvents.length) {
      replayState.value = 'idle'
      replayCurrentTime.value = replayDuration.value
      replayProgress.value = replayDuration.value
      return
    }

    replayAnimFrame = requestAnimationFrame(tick)
  }

  replayAnimFrame = requestAnimationFrame(tick)
}

function replayPause() {
  replayState.value = 'paused'
  if (replayAnimFrame) {
    cancelAnimationFrame(replayAnimFrame)
    replayAnimFrame = 0
  }
}

function replaySeek(time: number) {
  replayPause()
  replayCurrentTime.value = time

  if (!replayTerm) return

  // Clear and replay all events up to the seek point
  replayTerm.reset()
  for (const ev of replayEvents) {
    if (ev[1] <= time) {
      replayTerm.write(ev[2])
    }
  }

  replayNextIdx = replayEvents.findIndex(e => e[1] > time)
  if (replayNextIdx < 0) replayNextIdx = replayEvents.length
}

function destroyReplay() {
  replayPause()
  if (replayTerm) {
    replayTerm.dispose()
    replayTerm = null
  }
  replayFitAddon = null
  replayEvents = []
  replayLoadError.value = false
  replayState.value = 'idle'
  replayProgress.value = 0
  replayCurrentTime.value = 0
}

function formatReplayTime(seconds: number): string {
  if (!isFinite(seconds) || isNaN(seconds)) return '0:00'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

// Clean up on unmount
onUnmounted(() => {
  rdpDestroy()
  destroyReplay()
})

// Expose open method for parent to call
defineExpose({ open })
</script>

<style scoped>
.replay-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
}

.replay-modal-fade-enter-active,
.replay-modal-fade-leave-active {
  transition: opacity var(--dcn-transition-normal);
}

.replay-modal-fade-enter-from,
.replay-modal-fade-leave-to {
  opacity: 0;
}

.replay-modal {
  display: flex;
  flex-direction: column;
  background: #1a1a1a;
  border-radius: var(--dcn-radius-lg);
  overflow: hidden;
  box-shadow: var(--dcn-shadow-xl);
}

.replay-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px var(--dcn-space-4);
  background: #252525;
  border-bottom: 1px solid #333;
  flex-shrink: 0;
  user-select: none;
}

.replay-modal-title {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  color: #ddd;
  font-size: var(--dcn-text-md);
  font-weight: 500;
}

.replay-modal-header-right {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.replay-modal-elapsed {
  color: #999;
  font-size: var(--dcn-text-base);
  font-family: var(--dcn-font-mono);
}

.replay-modal-close-btn {
  color: #888 !important;
  font-size: var(--dcn-text-xl);
}

.replay-modal-close-btn:hover {
  color: #fff !important;
}

.replay-modal-body {
  background: #000;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.replay-modal-display {
  width: 100%;
  height: 100%;
}

.replay-modal-display.is-rdp {
  overflow: hidden;
}

.replay-modal-display.is-rdp :deep(canvas) {
  display: block;
}

.replay-watermark {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 5;
  background-repeat: repeat;
  transform: rotate(-18deg);
  transform-origin: center;
  user-select: none;
}

.replay-modal-error {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--dcn-space-3);
  color: var(--dcn-danger);
}

.replay-modal-error p {
  margin: 0;
}

.replay-modal-controls {
  display: flex;
  align-items: center;
  padding: 10px var(--dcn-space-4);
  background: #252525;
  border-top: 1px solid #333;
  gap: var(--dcn-space-2);
  flex-shrink: 0;
}

.ctrl-btn {
  color: #ccc !important;
  font-size: var(--dcn-text-lg);
}

.ctrl-btn:hover {
  color: #fff !important;
}

.ctrl-btn.is-disabled {
  color: #555 !important;
}

.ctrl-slider {
  flex: 1;
  margin: 0 var(--dcn-space-2);
}

.ctrl-speed {
  width: 80px;
}

.replay-video-element {
  display: block;
  width: 100%;
  height: 100%;
  background: #000;
  object-fit: fill;
}

.replay-error-text {
  color: var(--dcn-text-secondary);
  text-align: center;
  padding: var(--dcn-space-10);
}
</style>
