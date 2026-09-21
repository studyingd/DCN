import { afterEach, describe, expect, it, vi } from 'vitest'
import { decodeJwtPayload, isJwtExpired } from '../jwt'

/** 构造 base64url 段(与真实 JWT 编码一致:去掉 + / 与 = 填充)。 */
function encodeSegment(value: object | string): string {
  const raw = typeof value === 'string' ? value : JSON.stringify(value)
  return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** 拼一个 header.payload.signature 三段式 token(签名不参与客户端解码,随意填)。 */
function makeToken(payload: object | string): string {
  return `${encodeSegment({ alg: 'HS256', typ: 'JWT' })}.${encodeSegment(payload)}.fake-signature`
}

/** 与真实后端 JWT 一致:payload 按 UTF-8 字节编码为 base64url(btoa 只接受 latin1,不够用)。 */
function encodeSegmentUtf8(payload: object): string {
  const bytes = new TextEncoder().encode(JSON.stringify(payload))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

afterEach(() => {
  vi.useRealTimers()
})

describe('decodeJwtPayload', () => {
  it('解码合法 token 的 payload', () => {
    const payload = {
      sub: '42',
      username: 'admin',
      role: 'admin',
      permissions: ['device:read', 'device:write'],
      exp: 1893456000,
      iat: 1717200000,
    }
    expect(decodeJwtPayload(makeToken(payload))).toEqual(payload)
  })

  it('还原 base64url 字符(- _)且容忍缺失的 = 填充', () => {
    // 该 payload 的标准 base64 同时含 + 与 /,编码为 base64url 后变成 - 与 _
    const payload = { s: 'subjects?_no+yes/ok' }
    const token = makeToken(payload)
    expect(token.split('.')[1]).toMatch(/[-_]/)
    expect(token.split('.')[1]).not.toContain('=')
    expect(decodeJwtPayload(token)).toEqual(payload)
  })

  it('非 token 字符串返回空对象', () => {
    expect(decodeJwtPayload('not-a-jwt')).toEqual({})
    expect(decodeJwtPayload('')).toEqual({})
    expect(decodeJwtPayload('onlyheader')).toEqual({})
  })

  it('payload 段非法 base64 时返回空对象', () => {
    expect(decodeJwtPayload('header.@@not-base64@@.sig')).toEqual({})
  })

  it('payload 段是合法 base64 但不是 JSON 时返回空对象', () => {
    expect(decodeJwtPayload(makeToken('hello world'))).toEqual({})
  })

  it('payload 为空对象时返回空对象', () => {
    expect(decodeJwtPayload(makeToken({}))).toEqual({})
  })

  it('回归:UTF-8 编码的非 ASCII claim 正确解码(裸 atob 按 latin1 会乱码)', () => {
    // 后端(PyJWT 等)按 UTF-8 编码 payload,stores/auth.ts 会直接读取 p.username
    const token = `eyJhbGciOiJIUzI1NiJ9.${encodeSegmentUtf8({ sub: '42', username: '管理员' })}.sig`
    expect(decodeJwtPayload(token)).toEqual({ sub: '42', username: '管理员' })
  })
})

describe('isJwtExpired', () => {
  it('以 exp*1000 与当前时间比较(固定时钟边界)', () => {
    // 2024-06-01T00:00:00Z → epoch 秒 = 1717200000
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2024-06-01T00:00:00Z'))
    const nowSec = 1717200000

    // Date.now() >= exp*1000 即视为过期:exp 恰为当前秒 → 过期(边界含等号)
    expect(isJwtExpired(makeToken({ exp: nowSec }))).toBe(true)
    expect(isJwtExpired(makeToken({ exp: nowSec - 1 }))).toBe(true)
    expect(isJwtExpired(makeToken({ exp: nowSec + 1 }))).toBe(false)
  })

  it('未来 exp 未过期,过去 exp 已过期(真实时钟)', () => {
    const nowSec = Math.floor(Date.now() / 1000)
    expect(isJwtExpired(makeToken({ exp: nowSec + 3600 }))).toBe(false)
    expect(isJwtExpired(makeToken({ exp: nowSec - 3600 }))).toBe(true)
  })

  it('缺少 exp 时按过期处理(安全兜底)', () => {
    expect(isJwtExpired(makeToken({ sub: '42' }))).toBe(true)
  })

  it('exp 为 0(falsy)时按过期处理', () => {
    expect(isJwtExpired(makeToken({ exp: 0 }))).toBe(true)
  })

  it('非法 token 解不出 payload,按过期处理', () => {
    expect(isJwtExpired('not-a-jwt')).toBe(true)
    expect(isJwtExpired('')).toBe(true)
  })
})
