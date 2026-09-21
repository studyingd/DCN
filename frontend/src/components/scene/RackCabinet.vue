<template>
  <div
    class="rack-cabinet"
    :class="{ 'is-open': opening, 'is-shelf': isShelf }"
    @click="onClick"
    @contextmenu.prevent="onContextmenu"
  >
    <!-- 顶部：状态灯 + 机柜名 -->
    <div class="cabinet-head">
      <span class="status-led" :class="ledClass" :title="ledTitle" />
      <span class="cabinet-name" :title="rack.name">{{ rack.name }}</span>
    </div>

    <!-- 柜体（3D 开门场景） -->
    <div class="cabinet-stage">
      <div class="cabinet-body">
        <!-- 门后内部：U 位设备预览（按状态着色） -->
        <div class="cabinet-interior" :style="{ '--cap': RACK_CAPACITY_U }">
          <div
            v-for="(u, i) in interiorUnits"
            :key="i"
            class="interior-unit"
            :style="{
              top: u.top + '%',
              height: u.height + '%',
              background: u.color,
              boxShadow: '0 0 5px ' + u.color,
              opacity: u.opacity,
            }"
          />
          <div v-if="deviceCount === 0" class="interior-empty">空</div>
        </div>

        <!-- 柜门（左侧铰链，点击转开） -->
        <div v-if="!isShelf" class="cabinet-door">
          <div class="door-glass" />
          <div class="door-handle" />
        </div>
      </div>
    </div>

    <!-- 底部铭牌 -->
    <div class="cabinet-plate">
      <el-tag :type="isShelf ? 'warning' : 'primary'" size="small" class="plate-tag">
        {{ isShelf ? '货架' : '机柜' }}
      </el-tag>
      <span class="plate-count">{{ deviceCount }} 台</span>
      <span v-if="!isShelf" class="plate-u">{{ usedU }}/{{ capacityU }}U</span>
    </div>
    <div v-if="!isShelf" class="plate-bar">
      <div class="plate-bar-fill" :style="{ width: usagePercent + '%', background: usageColor }" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Rack, Device } from '@/types'
import { RACK_CAPACITY_U, RACK_DEVICE_SIZE_U, resolveRackLayout } from '@/utils/rackLayout'

const props = defineProps<{
  rack: Rack
  opening?: boolean
}>()

const emit = defineEmits<{
  click: [rack: Rack]
  contextmenu: [event: MouseEvent, rack: Rack]
}>()

const isShelf = computed(() => props.rack.type === 'shelf')
const deviceList = computed<Device[]>(() => props.rack.devices ?? [])
const deviceCount = computed(() => deviceList.value.length)
const capacityU = computed(() => RACK_CAPACITY_U)
const usedU = computed(() => Math.min(RACK_CAPACITY_U, deviceList.value.length * RACK_DEVICE_SIZE_U))

const usagePercent = computed(() =>
  capacityU.value ? Math.min(100, Math.round((usedU.value / capacityU.value) * 100)) : 0,
)
const usageColor = computed(() => {
  const p = usagePercent.value
  if (p > 90) return 'var(--dcn-danger)'
  if (p > 70) return 'var(--dcn-warning)'
  return 'var(--dcn-success)'
})

// 机柜顶部状态灯：有离线→红；全部在线→绿；无设备→灰（status 是严格二值）
const ledClass = computed(() => {
  if (deviceCount.value === 0) return 'led-empty'
  const statuses = deviceList.value.map((d) => (d.status || '').toLowerCase())
  if (statuses.some((s) => s !== 'online')) return 'led-offline'
  return 'led-online'
})
const ledTitle = computed(
  () =>
    ({
      'led-online': '全部在线',
      'led-offline': '存在离线设备',
      'led-empty': '暂无设备',
    })[ledClass.value] ?? '',
)

function statusColor(status: string): string {
  return String(status || '').toLowerCase() === 'online' ? 'var(--dcn-dot-online)' : 'var(--dcn-dot-offline)'
}

// 门后 U 位预览：布局算法单一真源 utils/rackLayout.ts（与 Scene2D
// U 位明细共用，历史两份实现曾对冲突/越界数据渲染口径漂移）。
// 溢出设备(start=null,机柜已满)在预览中按满格尾部堆叠半透明示意。
const interiorUnits = computed(() => {
  const cap = capacityU.value
  const positioned = resolveRackLayout(deviceList.value)
  const units: { top: number; height: number; color: string; opacity: number }[] = []
  for (const item of positioned) {
    if (item.start == null) {
      // 溢出：固定贴在满格底部，半透明堆叠，不再越界渲染
      units.push({ top: 96, height: 4, color: statusColor(item.device.status), opacity: 0.35 })
      continue
    }
    units.push({
      top: ((item.start - 1) / cap) * 100,
      height: (RACK_DEVICE_SIZE_U / cap) * 100,
      color: statusColor(item.device.status),
      opacity: 0.85,
    })
  }
  return units
})

function onClick() {
  emit('click', props.rack)
}
function onContextmenu(e: MouseEvent) {
  emit('contextmenu', e, props.rack)
}
</script>

<style scoped>
.rack-cabinet {
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, var(--dcn-bg-card) 0%, var(--dcn-bg-section) 100%);
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-lg);
  padding: var(--dcn-space-3);
  cursor: pointer;
  user-select: none;
  transition:
    transform var(--dcn-transition-fast),
    box-shadow var(--dcn-transition-fast),
    border-color var(--dcn-transition-fast);
}
.rack-cabinet:hover {
  transform: translateY(-2px);
  border-color: var(--dcn-primary);
  box-shadow:
    var(--dcn-shadow-lg),
    0 0 20px var(--dcn-primary-bg-deep);
}

/* ── 顶部：状态灯 + 名称 ── */
.cabinet-head {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-2);
}
.status-led {
  width: 8px;
  height: 8px;
  border-radius: var(--dcn-radius-full);
  flex-shrink: 0;
}
.led-online {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 6px var(--dcn-dot-online);
  animation: led-breathe 2.4s ease-in-out infinite;
}
.led-offline {
  background: var(--dcn-dot-offline);
  box-shadow: 0 0 6px var(--dcn-dot-offline);
}
.led-empty {
  background: var(--dcn-text-placeholder);
}
@keyframes led-breathe {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.cabinet-name {
  font-size: var(--dcn-text-sm);
  font-weight: 600;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 柜体（3D 开门场景） ── */
.cabinet-stage {
  perspective: 1100px;
  flex: 1;
  display: flex;
}
.cabinet-body {
  position: relative;
  width: 100%;
  min-height: 190px;
  transform-style: preserve-3d;
  background: var(--dcn-bg-page);
  border: 2px solid var(--dcn-rack-bg);
  border-radius: var(--dcn-radius-md);
  overflow: hidden;
}

/* 门后内部：U 位刻度 + 设备预览 */
.cabinet-interior {
  position: absolute;
  inset: 4px;
  background-image: repeating-linear-gradient(
    to bottom,
    transparent 0,
    transparent calc(100% / var(--cap, 24) - 1px),
    var(--dcn-border-light) calc(100% / var(--cap, 24) - 1px),
    var(--dcn-border-light) calc(100% / var(--cap, 24))
  );
}
.interior-unit {
  position: absolute;
  left: 6px;
  right: 6px;
  border-radius: 1px;
  opacity: 0.85;
}
.interior-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}

/* ── 柜门（左侧铰链，点击转开） ── */
.cabinet-door {
  position: absolute;
  inset: 0;
  transform: rotateY(0deg); /* 初始态：确保到 is-open 的 rotateY 有过渡插值 */
  transform-origin: left center;
  transform-style: preserve-3d;
  backface-visibility: hidden;
  transition:
    transform 0.55s cubic-bezier(0.22, 0.61, 0.36, 1),
    opacity 0.55s ease;
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-sm);
  background: linear-gradient(135deg, rgba(63, 63, 70, 0.55) 0%, rgba(28, 28, 40, 0.72) 100%);
  backdrop-filter: blur(1px);
}
.door-glass {
  position: absolute;
  inset: 6px;
  border-radius: var(--dcn-radius-xs);
  background: linear-gradient(120deg, rgba(255, 255, 255, 0.07) 0%, transparent 42%);
  border: 1px solid rgba(255, 255, 255, 0.05);
}
.door-handle {
  position: absolute;
  right: 5px;
  top: 50%;
  width: 4px;
  height: 30px;
  transform: translateY(-50%);
  background: var(--dcn-border-strong);
  border-radius: var(--dcn-radius-xs);
}
.rack-cabinet.is-open .cabinet-door {
  transform: rotateY(-102deg);
  opacity: 0.18;
}

/* 货架：无门，开放架样式 */
.rack-cabinet.is-shelf .cabinet-body {
  border-style: dashed;
}

/* ── 底部铭牌 ── */
.cabinet-plate {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-top: var(--dcn-space-2);
}
.plate-tag {
  flex-shrink: 0;
}
.plate-count,
.plate-u {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  white-space: nowrap;
}
.plate-bar {
  margin-top: var(--dcn-space-1);
  height: 4px;
  background: var(--dcn-border);
  border-radius: var(--dcn-radius-xs);
  overflow: hidden;
}
.plate-bar-fill {
  height: 100%;
  border-radius: var(--dcn-radius-xs);
  transition: width 0.3s ease;
}
</style>
