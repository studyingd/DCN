import { describe, expect, it } from 'vitest'
import { ITEM_LABELS, getItemLabel } from '../inspectionLabels'

// 键集必须与后端 inspection_commands.py 的 item_type 一致；
// 后端侧由 backend/tests/test_inspection_label_parity.py 钉住同一份契约。
describe('ITEM_LABELS', () => {
  it('覆盖全部 18 个后端 item_type', () => {
    expect(Object.keys(ITEM_LABELS)).toHaveLength(18)
  })

  it('网络设备专属 item_type 已随设备类型下线而移除', () => {
    // 交换机/路由器/防火墙不再纳管，后端 NETWORK_COMMANDS 已删除，
    // 这些键不能再出现，否则会与主机巡检项产生歧义。
    for (const retired of [
      'interface',
      'version',
      'routes',
      'log',
      'environment',
      'power',
      'fan',
      'stp',
      'vlan',
      'arp',
      'mac',
    ]) {
      expect(ITEM_LABELS).not.toHaveProperty(retired)
    }
    expect(ITEM_LABELS.vlan).toBeUndefined()
    expect(getItemLabel('vlan')).toBe('vlan')
  })

  it('Linux 主机项标签正确', () => {
    expect(ITEM_LABELS.cpu).toBe('CPU 使用率')
    expect(ITEM_LABELS.memory).toBe('内存使用')
    expect(ITEM_LABELS.disk).toBe('磁盘使用')
    expect(ITEM_LABELS.load).toBe('系统负载')
    expect(ITEM_LABELS.network).toBe('网络配置')
    expect(ITEM_LABELS.ports).toBe('监听端口')
    expect(ITEM_LABELS.processes).toBe('进程列表')
    expect(ITEM_LABELS.os_version).toBe('系统版本')
    expect(ITEM_LABELS.logs).toBe('异常日志')
    expect(ITEM_LABELS.failed_services).toBe('失败服务')
    // 注意：这是主机巡检项（检查服务器自身防火墙状态），与已下线的设备类型 firewall 无关。
    expect(ITEM_LABELS.firewall).toBe('防火墙')
    expect(ITEM_LABELS.security_updates).toBe('安全更新')
    expect(ITEM_LABELS.logins).toBe('登录记录')
  })

  it('Windows 主机项标签正确', () => {
    expect(ITEM_LABELS.system_info).toBe('系统信息')
    // 后端改查 SCM 失败事件(7000/7009/7031…)后，这一项数的是真故障，
    // 不再是「停着的自动服务」，所以文案与 Linux 的 failed_services 分开。
    expect(ITEM_LABELS.services).toBe('服务异常')
    expect(ITEM_LABELS.event_logs).toBe('事件日志')
    expect(ITEM_LABELS.updates).toBe('已装补丁')
    expect(ITEM_LABELS.uptime).toBe('运行时间')
  })

  it('不存在已废弃的 *_usage / *_status 命名空间', () => {
    // 这些键曾经存在于本文件里，但后端从未产出过，属于凭空虚构的命名空间。
    for (const stale of ['cpu_usage', 'memory_usage', 'disk_usage', 'bgp_status', 'nat_rules']) {
      expect(ITEM_LABELS).not.toHaveProperty(stale)
    }
  })

  it('所有键为 snake_case，所有值为非空字符串', () => {
    for (const [key, label] of Object.entries(ITEM_LABELS)) {
      expect(key).toMatch(/^[a-z][a-z0-9_]*$/)
      expect(typeof label).toBe('string')
      expect(label.length).toBeGreaterThan(0)
    }
  })

  it('failed_services 与 services 是两个平台的同类项，但文案已分开', () => {
    // 两者共用同一套阈值(1 warning / 3 critical)，键集也都被后端契约测试钉住；
    // 但 Linux 查 systemctl --failed(失败单元)，Windows 查 SCM 失败事件，
    // 语义不再完全等价，所以文案不再要求一致。
    expect(ITEM_LABELS.failed_services).toBe('失败服务')
    expect(ITEM_LABELS.services).toBe('服务异常')
    expect(ITEM_LABELS.failed_services).not.toBe(ITEM_LABELS.services)
  })
})

describe('getItemLabel', () => {
  it('已知 item_type 返回中文标签', () => {
    expect(getItemLabel('cpu')).toBe('CPU 使用率')
    expect(getItemLabel('uptime')).toBe('运行时间')
  })

  it('未知 item_type 原样返回，新增后端项不会渲染成空白', () => {
    expect(getItemLabel('brand_new_item')).toBe('brand_new_item')
  })

  it('空字符串原样返回', () => {
    expect(getItemLabel('')).toBe('')
  })

  it('回归：原型链键不命中 Object.prototype 成员，原样返回', () => {
    // 修复前 `ITEM_LABELS[type] || type` 会把 constructor 解析成 Object 构造函数，
    // 模板里渲染出 "function Object() { [native code] }"。
    for (const key of ['constructor', 'toString', 'valueOf', 'hasOwnProperty', '__proto__']) {
      expect(getItemLabel(key)).toBe(key)
    }
  })
})
