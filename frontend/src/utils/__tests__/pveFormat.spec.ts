import { describe, expect, it } from 'vitest'

import { validateSnapName } from '../pveFormat'

describe('validateSnapName（PVE 快照名口径）', () => {
  it('合法名:字母/数字/下划线', () => {
    expect(validateSnapName('before_upgrade')).toBeNull()
    expect(validateSnapName('snap20260917')).toBeNull()
    expect(validateSnapName('A_1')).toBeNull()
  })

  it('连字符/中文/空格/斜杠等非法字符提前拦下(与后端同口径)', () => {
    // 连字符曾是前端占位文案里的示例,但 PVE 本身不接受
    expect(validateSnapName('before-upgrade')).toMatch(/字母、数字、下划线/)
    expect(validateSnapName('快照')).toMatch(/字母、数字、下划线/)
    expect(validateSnapName('with space')).toMatch(/字母、数字、下划线/)
    expect(validateSnapName('a/b')).toMatch(/字母、数字、下划线/)
  })

  it('current 是 PVE 保留名', () => {
    expect(validateSnapName('current')).toMatch(/保留名/)
    expect(validateSnapName('CURRENT')).toMatch(/保留名/)
  })

  it('空名与超长名', () => {
    expect(validateSnapName('')).toMatch(/请输入/)
    expect(validateSnapName('   ')).toMatch(/请输入/)
    expect(validateSnapName('a'.repeat(65))).toMatch(/过长/)
    expect(validateSnapName('a'.repeat(64))).toBeNull()
  })
})
