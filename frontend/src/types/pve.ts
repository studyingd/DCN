export interface PveConnection {
  id: number
  name: string
  host: string
  port: number
  token_id: string
  verify_ssl: number
  enabled: number
  description: string | null
  created_at: string | null
}

export interface PveNode {
  node: string
  status: string
  cpu?: number
  maxcpu?: number
  mem?: number
  maxmem?: number
  level?: string
  [key: string]: unknown
}

export interface PveFilesystemUsage {
  mount: string
  filesystem_type?: string | null
  total_bytes: number
  used_bytes?: number | null
}

export interface PveGuest {
  node: string
  type: 'qemu'
  vmid: number
  name: string
  status: string
  cpu?: number
  cpus?: number
  maxcpu?: number
  mem?: number
  maxmem?: number
  disk?: number
  maxdisk?: number
  uptime?: number
  template?: number
  diskread?: number
  diskwrite?: number
  netin?: number
  netout?: number
  disk_read_rate?: number
  disk_write_rate?: number
  net_in_rate?: number
  net_out_rate?: number
  guest_agent?: {
    qga_enabled: boolean
    qga_available: boolean
    qga_ip_address?: string | null
    qga_os_name?: string | null
    qga_disks?: PveFilesystemUsage[]
    filesystem_available?: boolean
    filesystem_source?: 'qga' | 'ssh' | 'winrm' | null
    filesystem_disks?: PveFilesystemUsage[]
    filesystem_error?: string | null
  }
  disk_rate_unavailable?: boolean
  net_rate_unavailable?: boolean
  [key: string]: unknown
}

export type PveGuestOs = 'linux' | 'windows' | 'unknown'

export interface PveGuestBinding {
  id: number | null
  connection_id: number
  guest_type: 'qemu'
  vmid: number
  ip_address: string | null
  os_system: PveGuestOs
  // true 表示 os_system=linux 是后端排除法兜底(未确证),打开绑定窗口会自动探测纠正
  os_assumed?: boolean
  // 持久化的精确 OS 名(QGA 不可用时的兜底,迁移 0038);QGA 实时 qga_os_name 优先
  os_name?: string | null
  ssh_port: number
  winrm_port: number
  username: string | null
  has_password: boolean
  has_ssh_key: boolean
  enabled: boolean
  configured: boolean
  last_test_status: string | null
  last_test_error: string | null
  last_test_at: string | null
  qga_enabled: boolean
  qga_available: boolean
  qga_ip_address: string | null
  qga_os_system?: 'linux' | 'windows' | null
  qga_os_name?: string | null
  qga_disks: PveFilesystemUsage[]
  winrm_available: boolean
}

export interface PveRrdPoint {
  time: number
  cpu?: number
  mem?: number
  maxmem?: number
  diskread?: number
  diskwrite?: number
  netin?: number
  netout?: number
  [key: string]: unknown
}

export interface PveSnapshot {
  name: string
  description?: string
  snaptime?: number
  vmstate?: number
  parent?: string
  [key: string]: unknown
}

export interface PveGuestCreate {
  vmid?: number
  name: string
  cores: number
  memory_mb: number
  disk_gb: number
  storage: string
  iso?: string
  /** 默认 true:QGA 是 IP/OS/磁盘采集自动化链路的开关 */
  agent?: boolean
}

/** 虚机引用的一个磁盘/光驱/挂载。kind 决定销毁虚机时数据会不会一起被清掉。 */
export interface PveGuestVolume {
  key: string
  volid: string
  size_bytes: number | null
  /** disk=虚机磁盘卷 · unused=残留卷 · cdrom=ISO 介质 · bind=LXC 宿主机绑定挂载 */
  kind: 'disk' | 'unused' | 'cdrom' | 'bind'
}

/** 删除前预览：会清掉哪些卷、会顺带清掉哪些平台侧引用。 */
export interface PveGuestVolumePreview {
  node: string
  status: string
  running: boolean
  template: boolean
  volumes: PveGuestVolume[]
  purged_local: {
    bindings: number
    containers: number
    business_links: number
  }
}

export interface PveGuestDeleteResult {
  success: boolean
  message: string
  task?: string | null
  node: string | null
  stopped_first: boolean
  volumes: PveGuestVolume[]
  purged_local: {
    bindings: number
    containers: number
    docker_status: number
    business_links: number
  }
}

/** PVE 异步任务(创建/克隆/销毁/快照)的执行状态。 */
export interface PveTaskStatus {
  /** running | stopped */
  status: 'running' | 'stopped'
  /** PVE 终态结论:OK 或错误文本;运行中为 null */
  exitstatus: string | null
  /** 任务是否成功(仅终态有意义) */
  ok: boolean
  /** 是否已到终态 */
  done: boolean
}

/** 创建虚机/容器的响应(任务提交回执)。 */
export interface PveGuestCreateResult {
  success: boolean
  vmid: number
  message: string
  task: string | null
  node: string
}

/** 克隆虚机/容器的响应(任务提交回执)。 */
export interface PveGuestCloneResult {
  success: boolean
  newid: number
  message: string
  task: string | null
  node: string
}

/** 节点存储(新建虚机的存储下拉数据源)。 */
export interface PveNodeStorage {
  storage: string
  type: string
  /** 逗号分隔的 content 列表,如 "images,iso" */
  content: string
  active: number
  total?: number
  used?: number
  avail?: number
}

/** 调整虚机规格的请求(未提供的字段不变;磁盘只允许扩容)。 */
export interface PveGuestConfigUpdate {
  cores?: number
  memory_mb?: number
  /** 磁盘目标总量(GB),必须大于当前容量 */
  disk_gb?: number
}

/** 调整规格的响应(带最新 config/status 供刷新)。 */
export interface PveGuestConfigUpdateResult {
  success: boolean
  message: string
  config: Record<string, unknown>
  disk_bytes: number | null
  status: Record<string, unknown>
}
