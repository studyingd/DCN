/**
 * Shared inspection item type labels.
 * Single source of truth — import from here instead of duplicating.
 */

export const ITEM_LABELS: Record<string, string> = {
  cpu_usage: 'CPU 使用率',
  memory_usage: '内存使用率',
  disk_usage: '磁盘使用率',
  disk_io: '磁盘 I/O',
  network_io: '网络 I/O',
  process_count: '进程数',
  uptime: '运行时间',
  os_version: '系统版本',
  load_average: '负载均值',
  temperature: '温度',
  fan_status: '风扇状态',
  power_status: '电源状态',
  interface_status: '接口状态',
  route_table: '路由表',
  arp_table: 'ARP 表',
  vlan_info: 'VLAN 信息',
  firewall_rules: '防火墙规则',
  nat_rules: 'NAT 规则',
  vpn_status: 'VPN 状态',
  bgp_status: 'BGP 状态',
  ospf_status: 'OSPF 状态',
  dhcp_status: 'DHCP 状态',
  dns_status: 'DNS 状态',
  ntp_status: 'NTP 状态',
  log_check: '日志检查',
  security_check: '安全检查',
  backup_status: '备份状态',
}
