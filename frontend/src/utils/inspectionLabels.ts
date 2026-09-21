/**
 * Shared inspection item type labels.
 * Single source of truth — import from here instead of duplicating mappings.
 *
 * The keys MUST match the backend `item_type` values produced by
 * `backend/app/services/inspection_commands.py` (LINUX_COMMANDS /
 * WINDOWS_COMMANDS) — that is the namespace stored in
 * `inspection_item_results.item_type` and rendered here.
 * `backend/tests/test_inspection_label_parity.py` pins the key set on both
 * sides, so adding an inspection item without a label fails CI.
 *
 * Label *text* may legitimately differ from the backend's own ITEM_LABELS
 * (the backend wording is used for the custom-mode checkbox catalog, these are
 * sized for table columns); only the key set is a contract.
 */

export const ITEM_LABELS: Record<string, string> = {
  // Linux 主机
  cpu: 'CPU 使用率',
  memory: '内存使用',
  disk: '磁盘使用',
  load: '系统负载',
  network: '网络配置',
  ports: '监听端口',
  processes: '进程列表',
  os_version: '系统版本',
  logs: '异常日志',
  failed_services: '失败服务',
  firewall: '防火墙',
  security_updates: '安全更新',
  logins: '登录记录',
  // Windows 主机
  system_info: '系统信息',
  // 后端改查 SCM 失败事件（7000/7009/7031…）后，这一项统计的是「真的挂了的服务」，
  // 不再是「停着的自动服务」，所以叫「服务异常」而不是「失败服务」。
  services: '服务异常',
  event_logs: '事件日志',
  updates: '已装补丁',
  uptime: '运行时间',
}

/**
 * Human-readable label for an inspection `item_type`.
 * Unknown types fall back to the raw key so new backend items stay visible
 * instead of rendering as blank. Uses hasOwnProperty because a plain
 * `ITEM_LABELS[type] || type` would resolve prototype keys — e.g.
 * `getItemLabel('constructor')` would return the Object constructor.
 */
export function getItemLabel(type: string): string {
  return Object.prototype.hasOwnProperty.call(ITEM_LABELS, type) ? ITEM_LABELS[type] : type
}
