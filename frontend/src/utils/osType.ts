/**
 * 操作系统判定——全项目统一口径。
 *
 * 约定：``os_system`` 里含 "windows" 就走 RDP，其余一律按 SSH 处理。
 * 必须用「排除法」而不是「要求含 linux」：``os_system`` 存的是探测/手填的
 * 真实发行版名，例如 ``Ubuntu 3ubuntu0.17``、``Debian 7+deb13u4``、
 * ``Rocky Linux 10.0``——前两个并不包含 "linux" 字样。
 *
 * 曾经只有 DeviceDetail.vue 用了「必须含 linux」的正向匹配，导致云服务器的
 * SSH/RDP 两个按钮都不渲染（用户看到的"云服务器无法远程"就是这个）。
 * TerminalView / ScriptPanel / InspectionPanel 一直是排除法，此处与它们对齐。
 */

export function isWindowsOs(os: string | null | undefined): boolean {
  return (os || '').toLowerCase().includes('windows')
}

/** 可走 SSH 的目标：非 Windows 即可，未知系统也默认给 SSH（与 TerminalView 一致）。 */
export function isSshOs(os: string | null | undefined): boolean {
  return !isWindowsOs(os)
}
