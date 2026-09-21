import { describe, expect, it } from 'vitest'
import { DEVICE_TYPE_LABELS, OPS_DEVICE_TYPES, deviceTypeLabel, isOpsDevice } from '../deviceLabels'

describe('DEVICE_TYPE_LABELS', () => {
  it('只覆盖纳管中的 3 种主机类设备', () => {
    expect(DEVICE_TYPE_LABELS).toEqual({
      server: '服务器',
      cloud_server: '云服务器',
      host: '台式主机',
    })
  })

  it('机房管理可选的三种类型齐全', () => {
    for (const selectable of ['server', 'cloud_server', 'host']) {
      expect(DEVICE_TYPE_LABELS).toHaveProperty(selectable)
    }
  })

  it('交换机/路由器/防火墙已彻底下线，映射表不再包含', () => {
    for (const retired of ['switch', 'router', 'firewall']) {
      expect(DEVICE_TYPE_LABELS).not.toHaveProperty(retired)
    }
  })
})

describe('deviceTypeLabel', () => {
  it.each(Object.entries(DEVICE_TYPE_LABELS))('已知类型 %s 返回中文标签', (type, label) => {
    expect(deviceTypeLabel(type)).toBe(label)
  })

  it('未知类型原样返回', () => {
    expect(deviceTypeLabel('printer')).toBe('printer')
  })

  it('已下线的网络设备类型不再被识别，回退成原始字符串', () => {
    expect(deviceTypeLabel('switch')).toBe('switch')
    expect(deviceTypeLabel('router')).toBe('router')
    expect(deviceTypeLabel('firewall')).toBe('firewall')
  })

  it('大小写不敏感命中已知类型', () => {
    expect(deviceTypeLabel('Server')).toBe('服务器')
    expect(deviceTypeLabel('Cloud_Server')).toBe('云服务器')
    expect(deviceTypeLabel('HOST')).toBe('台式主机')
  })

  it('未命中时保留调用方传入的原始大小写', () => {
    expect(deviceTypeLabel('PDU')).toBe('PDU')
    expect(deviceTypeLabel('MinIO')).toBe('MinIO')
  })

  it('空字符串原样返回', () => {
    expect(deviceTypeLabel('')).toBe('')
  })

  it('回归:原型链键不命中 Object.prototype 成员,原样返回', () => {
    expect(deviceTypeLabel('constructor')).toBe('constructor')
    expect(deviceTypeLabel('toString')).toBe('toString')
    expect(deviceTypeLabel('valueOf')).toBe('valueOf')
    expect(deviceTypeLabel('__proto__')).toBe('__proto__')
    expect(deviceTypeLabel('hasOwnProperty')).toBe('hasOwnProperty')
  })
})

describe('OPS_DEVICE_TYPES / isOpsDevice', () => {
  it('与后端 OPS_TARGET_TYPES 一致：三种纳管类型全部可运维', () => {
    expect([...OPS_DEVICE_TYPES].sort()).toEqual(['cloud_server', 'host', 'server'])
    expect(OPS_DEVICE_TYPES).toEqual(Object.keys(DEVICE_TYPE_LABELS))
  })

  it('云服务器必须被当成可运维设备', () => {
    // 回归：早期各模块写死 ['server','host']，云服务器的指标区/告警/容器全部失效
    expect(isOpsDevice('cloud_server')).toBe(true)
    expect(isOpsDevice('Cloud_Server')).toBe(true)
    expect(isOpsDevice('server')).toBe(true)
    expect(isOpsDevice('host')).toBe(true)
  })

  it('已下线类型与非法输入不可运维', () => {
    expect(isOpsDevice('switch')).toBe(false)
    expect(isOpsDevice('router')).toBe(false)
    expect(isOpsDevice('')).toBe(false)
    expect(isOpsDevice(undefined)).toBe(false)
    expect(isOpsDevice(null)).toBe(false)
    // 原型链键不得意外命中
    expect(isOpsDevice('constructor')).toBe(false)
  })
})
