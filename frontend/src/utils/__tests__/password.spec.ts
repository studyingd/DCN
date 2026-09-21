import { describe, expect, it } from 'vitest'
import { isStrongPassword, passwordStrengthError } from '../password'

describe('passwordStrengthError（与后端 validators.py 同口径）', () => {
  it('少于 8 位直接拒', () => {
    expect(passwordStrengthError('Ab1!')).toBe('密码长度不能少于8个字符')
  })

  it('8 位但只有两类字符也拒——这正是"创建用户失败"的常见真因', () => {
    expect(passwordStrengthError('12345678')).toContain('至少3种')
    expect(passwordStrengthError('abcdefgh')).toContain('至少3种')
  })

  it('三类字符即通过', () => {
    // 小写+数字+特殊 / 大写+小写+数字
    expect(passwordStrengthError('abcd123!')).toBe('')
    expect(passwordStrengthError('Abcd1234')).toBe('')
    // 只有两类（小写+数字、小写+特殊）仍然不够
    expect(passwordStrengthError('abcd1234')).toContain('至少3种')
    expect(passwordStrengthError('abcd!@#%')).toContain('至少3种')
    // 注意：后端的特殊字符集不含 `$`，别拿它凑第三类
    expect(passwordStrengthError('abcd!@#$')).toContain('至少3种')
  })

  it('空值按长度不足处理，由表单的必填规则先拦', () => {
    expect(passwordStrengthError('')).toContain('不能少于')
    expect(isStrongPassword('Abcd1234')).toBe(true)
  })
})
