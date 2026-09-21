import { describe, expect, it } from 'vitest'
import { isSshOs, isWindowsOs } from '../osType'

describe('isWindowsOs / isSshOs（全项目统一口径）', () => {
  it('Windows 各种写法都识别为 RDP', () => {
    expect(isWindowsOs('Microsoft Windows Server 2019 Datacenter')).toBe(true)
    expect(isWindowsOs('windows')).toBe(true)
    expect(isWindowsOs('Windows 11 Pro')).toBe(true)
    expect(isSshOs('Microsoft Windows Server 2019 Datacenter')).toBe(false)
  })

  it('关键回归：发行版名不含 "linux" 也必须给 SSH（云服务器踩过的坑）', () => {
    // 库里真实数据：cloud_server 的 os_system 是 "Ubuntu 3ubuntu0.17"
    expect(isSshOs('Ubuntu 3ubuntu0.17')).toBe(true)
    expect(isSshOs('Debian 7+deb13u4')).toBe(true)
    expect(isSshOs('CentOS 7.9')).toBe(true)
    expect(isWindowsOs('Ubuntu 3ubuntu0.17')).toBe(false)
  })

  it('显式含 linux 字样的照常识别', () => {
    expect(isSshOs('linux')).toBe(true)
    expect(isSshOs('Rocky Linux 10.0')).toBe(true)
  })

  it('系统未知时默认 SSH，与 TerminalView 的兜底一致', () => {
    expect(isSshOs('')).toBe(true)
    expect(isSshOs(null)).toBe(true)
    expect(isSshOs(undefined)).toBe(true)
    expect(isWindowsOs('')).toBe(false)
    expect(isWindowsOs(null)).toBe(false)
  })
})
