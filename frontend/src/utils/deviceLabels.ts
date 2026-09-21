/**
 * Shared device type labels and helper functions.
 * Single source of truth — import from here instead of duplicating mappings.
 */

export const DEVICE_TYPE_LABELS: Record<string, string> = {
  server: '服务器',
  cloud_server: '云服务器',
  host: '台式主机',
  // 仅纳管主机类设备；交换机/路由器/防火墙已彻底下线，未命中类型由 deviceTypeLabel 原样回退。
}

/**
 * 具备远程运维通道(Linux=SSH、Windows=RDP/WinRM)、可被监控与自动化纳管的设备类型。
 * 必须与后端 app/models/device.py 的 OPS_TARGET_TYPES 保持一致——云服务器与物理
 * 服务器走同一套通道，不能因为类型不同而在指标/容器/业务/告警里被静默过滤掉。
 */
export const OPS_DEVICE_TYPES = ['server', 'cloud_server', 'host'] as const

export type OpsDeviceType = (typeof OPS_DEVICE_TYPES)[number]

/** 大小写不敏感判断设备类型是否可纳管，与 deviceTypeLabel 的匹配规则一致。 */
export function isOpsDevice(type?: string | null): boolean {
  const key = type?.toLowerCase()
  return !!key && (OPS_DEVICE_TYPES as readonly string[]).includes(key)
}

/**
 * 设备角色描述（机房 U 位视图 chassis 铭牌用）。
 */
export const DEVICE_TYPE_ROLES: Record<string, string> = {
  server: '计算与存储',
  cloud_server: '云端算力',
  host: '终端主机',
}

export function deviceTypeRole(type: string): string {
  const key = type?.toLowerCase()
  return Object.prototype.hasOwnProperty.call(DEVICE_TYPE_ROLES, key) ? DEVICE_TYPE_ROLES[key] : '基础设施'
}

/**
 * Element Plus 标签色（shelf 设备列表等处的 el-tag type 属性）。
 */
export function deviceTypeTagType(type: string): 'info' | 'primary' {
  return String(type).toLowerCase() === 'cloud_server' ? 'primary' : 'info'
}

export function deviceTypeLabel(type: string): string {
  // Case-insensitive own-property lookup; misses (including prototype keys like
  // 'constructor') fall back to the caller's original string, casing intact.
  const key = type?.toLowerCase()
  return Object.prototype.hasOwnProperty.call(DEVICE_TYPE_LABELS, key) ? DEVICE_TYPE_LABELS[key] : type
}
