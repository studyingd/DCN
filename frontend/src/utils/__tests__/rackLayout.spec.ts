import { describe, expect, it } from 'vitest'
import {
  RACK_CAPACITY_U,
  RACK_DEVICE_SIZE_U,
  RACK_SLOT_STARTS,
  nextFreeSlot,
  resolveRackLayout,
  snapSlotStart,
} from '../rackLayout'
import type { Device } from '../../types'

function dev(id: number, position_u: number | null): Device {
  return {
    id,
    rack_id: 1,
    name: `dev-${id}`,
    type: 'server',
    ip_address: '127.0.0.1',
    os_system: '',
    status: 'online',
    position_u,
    size_u: 2,
    ssh_port: 22,
    rdp_port: 3389,
    winrm_port: 5985,
    created_at: '',
  }
}

describe('rackLayout 常量', () => {
  it('槽位序列为奇数 1..23，共 12 槽', () => {
    expect(RACK_CAPACITY_U).toBe(24)
    expect(RACK_DEVICE_SIZE_U).toBe(2)
    expect(RACK_SLOT_STARTS).toHaveLength(12)
    expect(RACK_SLOT_STARTS[0]).toBe(1)
    expect(RACK_SLOT_STARTS[11]).toBe(23)
  })

  it('snapSlotStart 吸附偶数到前一奇数槽、越界钳到边界', () => {
    expect(snapSlotStart(4)).toBe(3)
    expect(snapSlotStart(1)).toBe(1)
    expect(snapSlotStart(0)).toBe(1)
    expect(snapSlotStart(-5)).toBe(1)
    expect(snapSlotStart(24)).toBe(23)
    expect(snapSlotStart(99)).toBe(23)
  })
})

describe('resolveRackLayout', () => {
  it('显式 position_u 优先且吸附奇数', () => {
    const out = resolveRackLayout([dev(1, 5), dev(2, 8)])
    expect(out.map((p) => [p.device.id, p.start])).toEqual([
      [1, 5],
      [2, 7], // 8 吸附到 7
    ])
  })

  it('position_u=null 自动顺延到空槽', () => {
    const out = resolveRackLayout([dev(1, 1), dev(2, null), dev(3, null)])
    expect(out.map((p) => p.start)).toEqual([1, 3, 5])
  })

  it('显式槽位冲突时后到者降级为自动顺延（不重叠）', () => {
    const out = resolveRackLayout([dev(1, 3), dev(2, 3), dev(3, null)])
    // dev(2,3) 与 dev(1,3) 冲突 → 顺延到槽 1；dev(3,null) 顺延到槽 5
    const starts = out.map((p) => p.start)
    expect(starts).toEqual([1, 3, 5])
    const ids = out.map((p) => p.device.id)
    expect(ids).toEqual([2, 1, 3])
  })

  it('12 台设备全满后溢出设备 start=null', () => {
    const devices = Array.from({ length: 14 }, (_, i) => dev(i + 1, null))
    const out = resolveRackLayout(devices)
    expect(out.filter((p) => p.start != null)).toHaveLength(12)
    expect(out.filter((p) => p.start == null)).toHaveLength(2)
  })

  it('输出按槽位升序，溢出行在最后', () => {
    const devices = Array.from({ length: 14 }, (_, i) => dev(i + 1, null))
    const out = resolveRackLayout(devices)
    const starts = out.map((p) => p.start)
    const sorted = [...starts].sort((a, b) => (a ?? 99) - (b ?? 99))
    expect(starts).toEqual(sorted)
  })
})

describe('nextFreeSlot', () => {
  it('空机柜返回首槽', () => {
    expect(nextFreeSlot([])).toBe(1)
  })

  it('跳过已占用槽', () => {
    expect(nextFreeSlot([dev(1, 1), dev(2, 3)])).toBe(5)
  })

  it('偶数位吸附后算占用', () => {
    expect(nextFreeSlot([dev(1, 2)])).toBe(3)
  })

  it('满柜返回 null', () => {
    const devices = Array.from({ length: 12 }, (_, i) => dev(i + 1, i * 2 + 1))
    expect(nextFreeSlot(devices)).toBeNull()
  })
})
