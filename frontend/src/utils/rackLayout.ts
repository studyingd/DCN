/**
 * 机柜 U 位布局的单一真源（前端渲染侧）。
 *
 * 语义：机柜固定 24U、每台设备固定占 2U、槽位对齐奇数（1,3,5...23）。
 * position_u=null 的设备自动顺延到第一个空槽；显式 position_u 冲突时
 * 后到者重排（后端落库时会做同样的归一化，两侧口径见
 * backend/app/routers/devices.py 的 _find_device_position——那边是
 * “分配并写库”，这边只管“把当前数据画出来”）。
 *
 * Scene2D.renderedRows 与 RackCabinet.interiorUnits 都从这里取结果，
 * 不允许再各写一份变体（历史上三份实现口径漂移过：冲突重叠渲染、
 * 顺延越界 >100% 等）。
 */

import type { Device } from '@/types'

export const RACK_CAPACITY_U = 24
export const RACK_DEVICE_SIZE_U = 2
/** 有效的 2U 起始槽位：1,3,5,…,23 */
export const RACK_SLOT_STARTS = Array.from(
  { length: (RACK_CAPACITY_U - RACK_DEVICE_SIZE_U) / 2 + 1 },
  (_, i) => 1 + i * RACK_DEVICE_SIZE_U,
)

/** 吸附到有效起始槽位（奇数、1..23），越界钳到边界。 */
export function snapSlotStart(u: number): number {
  const snapped = u % RACK_DEVICE_SIZE_U === 0 ? u - RACK_DEVICE_SIZE_U + 1 : u
  const first = RACK_SLOT_STARTS[0]
  const last = RACK_SLOT_STARTS[RACK_SLOT_STARTS.length - 1]
  return Math.max(first, Math.min(last, snapped))
}

/** 某台设备是否会被摆放在 slot 起始槽（前 2U）。 */
function slotOccupied(occupied: Set<number>, start: number): boolean {
  for (let offset = 0; offset < RACK_DEVICE_SIZE_U; offset += 1) {
    if (occupied.has(start + offset)) return true
  }
  return false
}

export interface PositionedDevice {
  device: Device
  /** 起始槽位（奇数，1..23）；越界/放不下时为 null，调用方自行处理 */
  start: number | null
}

/**
 * 把机架内设备解析成「起始槽位」序列：
 * 1. 显式 position_u 优先（吸附奇数）；
 * 2. null / 冲突的设备按 id 顺序顺延到第一个空槽；
 * 3. 全满时后续设备 start=null（渲染层可隐藏/告警，不再像旧实现那样
 *    溢出到 >100% 的位置重叠渲染）。
 */
export function resolveRackLayout(devices: Device[]): PositionedDevice[] {
  const occupied = new Set<number>()
  const explicit: PositionedDevice[] = []
  const pending: Device[] = []

  for (const device of [...devices].sort((a, b) => (a.position_u ?? 1) - (b.position_u ?? 1) || a.id - b.id)) {
    if (device.position_u == null) {
      pending.push(device)
      continue
    }
    const start = snapSlotStart(device.position_u)
    if (slotOccupied(occupied, start)) {
      pending.push(device) // 冲突降级为自动顺延
      continue
    }
    for (let offset = 0; offset < RACK_DEVICE_SIZE_U; offset += 1) occupied.add(start + offset)
    explicit.push({ device, start })
  }

  const auto: PositionedDevice[] = []
  let cursor = 0
  // 自动顺延顺序按 id 确定化（不依赖输入顺序/排序稳定性），保证同一批
  // 设备在任何调用路径下布局一致
  for (const device of [...pending].sort((a, b) => a.id - b.id)) {
    while (cursor < RACK_SLOT_STARTS.length && slotOccupied(occupied, RACK_SLOT_STARTS[cursor])) cursor += 1
    if (cursor >= RACK_SLOT_STARTS.length) {
      auto.push({ device, start: null })
      continue
    }
    const start = RACK_SLOT_STARTS[cursor]
    for (let offset = 0; offset < RACK_DEVICE_SIZE_U; offset += 1) occupied.add(start + offset)
    auto.push({ device, start })
  }

  // 输出按槽位排序（null 的排最后），显式与自动合并后稳定可渲染
  return [...explicit, ...auto].sort(
    (a, b) => (a.start ?? RACK_CAPACITY_U + 1) - (b.start ?? RACK_CAPACITY_U + 1) || a.device.id - b.device.id,
  )
}

/** 下一个空槽（无则 null）——新建设备的默认预设 U 位。 */
export function nextFreeSlot(devices: Device[]): number | null {
  const occupied = new Set<number>()
  for (const d of devices) {
    if (d.position_u == null) continue
    const start = snapSlotStart(d.position_u)
    occupied.add(start)
    occupied.add(start + 1)
  }
  return RACK_SLOT_STARTS.find((s) => !slotOccupied(occupied, s)) ?? null
}
