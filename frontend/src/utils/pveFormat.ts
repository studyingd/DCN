/**
 * PVE 虚拟机的展示格式化与判定纯函数。
 *
 * 这些函数原本内联在 `PvePanel.vue` 中，抽到这里以便：
 *  - 面板本体聚焦"数据加载 + 交互"，展示逻辑独立可测；
 *  - 多个虚机相关视图（详情抽屉 / 列表 / 快照）可复用同一套口径。
 *
 * 全部是纯函数：不依赖 Vue 响应式、不读写外部状态，可安全单测。
 */
import type { PveGuest, PveGuestBinding } from '@/types/pve'

// 虚拟机的 CPU 是相对分配核数、内存是相对 maxmem 的占用率,与物理机巡检语义不完全等价;
// 这里复用同一套阈值只是近似(逼近分配上限同样意味着风险),配色口径至少与指标页统一。
export function cpuPct(g: PveGuest): number {
  const realtimeUsage = Number(g.cpu)
  if (Number.isFinite(realtimeUsage) && realtimeUsage >= 0) {
    return Math.min(100, Math.round(realtimeUsage * 100))
  }
  if (!g.cpus || !g.maxcpu) return 0
  return Math.min(100, Math.round((Number(g.cpus) / Number(g.maxcpu)) * 100))
}

export function memPct(g: PveGuest): number {
  if (!g.mem || !g.maxmem) return 0
  return Math.min(100, Math.round((Number(g.mem) / Number(g.maxmem)) * 100))
}

export function fmtBytes(v: unknown): string {
  const n = Number(v) || 0
  const gb = n / (1024 * 1024 * 1024)
  if (gb >= 1) return `${gb.toFixed(1)}GB`
  return `${Math.round(n / (1024 * 1024))}MB`
}

export function formatGuestOs(binding: PveGuestBinding | null): string {
  if (!binding) return '未识别'
  // QGA 实时 pretty-name 最权威;其次是凭据探测持久化到 binding 的精确名
  // (QGA 不可用时由 detect-os / 自动化巡检回写,见迁移 0038)。
  if (binding.qga_available && binding.qga_os_name) return binding.qga_os_name
  if (binding.os_name) return binding.os_name
  if (binding.os_system === 'windows') return 'Windows（未获取版本）'
  if (binding.os_system === 'linux') return 'Linux（未获取发行版）'
  return '未识别'
}

export function diskPct(disk: { used_bytes?: number | null; total_bytes: number }): number {
  const total = Number(disk.total_bytes)
  const used = Number(disk.used_bytes)
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(used) || used < 0) return 0
  return Math.min(100, Math.round((used / total) * 100))
}

export function filesystemSourceLabel(source: 'qga' | 'ssh' | 'winrm' | null | undefined): string {
  if (source === 'qga') return 'QEMU Guest Agent'
  if (source === 'ssh') return 'Linux SSH'
  if (source === 'winrm') return 'Windows WinRM'
  return '未知来源'
}

export function sortedDisks(
  disks: { mount: string; filesystem_type?: string | null; total_bytes: number; used_bytes?: number | null }[],
): typeof disks {
  const opticalTypes = new Set(['cdfs', 'udf', 'iso9660'])
  return disks
    .filter((disk) => !opticalTypes.has(String(disk.filesystem_type || '').toLowerCase()))
    .sort((left, right) => left.mount.localeCompare(right.mount, 'en', { numeric: true, sensitivity: 'base' }))
}

export function fmtDiskBytes(v: unknown): string {
  return v === null || v === undefined ? '未知' : fmtBytes(v)
}

export function fmtRate(v: unknown, unavailable = false): string {
  if (unavailable) return '速率不可用'
  if (v === undefined || v === null || !Number.isFinite(Number(v))) return '采样中'
  const n = Math.max(0, Number(v))
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB/s`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB/s`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB/s`
  return `${Math.round(n)} B/s`
}

export function fmtUptime(sec: unknown): string {
  const s = Number(sec) || 0
  if (!s) return '-'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  return d > 0 ? `${d}天${h}时` : `${h}时${Math.floor((s % 3600) / 60)}分`
}

export function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

export function fmtClock(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString('zh-CN', { hour12: false })
}

export function isStale(ts: string | null, maxAgeSeconds = 30): boolean {
  return !!ts && Date.now() - new Date(ts).getTime() > maxAgeSeconds * 1000
}

export function key(g: PveGuest, action: string): string {
  return `${g.node}-${g.type}-${g.vmid}-${action}`
}

// PVE 模板(template=1)是克隆用的镜像,不能启动/电源操作/控制台/快照
export function isTemplate(g: PveGuest | null): boolean {
  return !!g && g.template === 1
}

// 可克隆:模板 或 已停止的虚机(运行中的虚机需先停止或打快照)
export function canClone(g: PveGuest | null): boolean {
  return !!g && (isTemplate(g) || g.status !== 'running')
}

// PVE 快照名只接受字母/数字/下划线(连字符也不行);current 是保留名。
// 与后端 routers/pve._validate_snapname 同一口径。
const SNAP_NAME_RE = /^[A-Za-z0-9_]+$/

/** 校验快照名:合法返回 null,否则返回中文错误提示。 */
export function validateSnapName(name: string): string | null {
  const value = name.trim()
  if (!value) return '请输入快照名'
  if (value.length > 64) return '快照名过长(最多 64 字符)'
  if (!SNAP_NAME_RE.test(value)) return '快照名仅支持字母、数字、下划线(不支持连字符/中文/空格)'
  if (value.toLowerCase() === 'current') return 'current 是 PVE 保留名,不能用作快照名'
  return null
}
