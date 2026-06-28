/**
 * Shared device type labels and helper functions.
 * Single source of truth — import from here instead of duplicating mappings.
 */

export const DEVICE_TYPE_LABELS: Record<string, string> = {
  server: '服务器',
  switch: '交换机',
  router: '路由器',
  firewall: '防火墙',
  host: '主机',
}

export function deviceTypeLabel(type: string): string {
  return DEVICE_TYPE_LABELS[type] || type
}

/** CSS variable names for device type colors (used with var(--dcn-device-xxx)) */
export const DEVICE_TYPE_COLOR_VARS: Record<string, string> = {
  server: 'var(--dcn-device-server)',
  switch: 'var(--dcn-device-switch)',
  router: 'var(--dcn-device-router)',
  firewall: 'var(--dcn-device-firewall)',
  host: 'var(--dcn-device-host)',
}

/** CSS variable names for device type background colors */
export const DEVICE_TYPE_BG_VARS: Record<string, string> = {
  server: 'var(--dcn-device-server-bg)',
  switch: 'var(--dcn-device-switch-bg)',
  router: 'var(--dcn-device-router-bg)',
  firewall: 'var(--dcn-device-firewall-bg)',
  host: 'var(--dcn-device-host-bg)',
}
