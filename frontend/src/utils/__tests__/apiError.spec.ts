import { describe, expect, it } from 'vitest'
import { extractErrorDetail, readHeader, readResponseHeader } from '../apiError'

/** 造一个 axios 风格的错误对象 */
function axiosError(data: unknown, headers?: Record<string, string>) {
  return { isAxiosError: true, response: { status: 422, data, headers }, message: 'Request failed' }
}

function jsonBlob(detail: string) {
  return new Blob([JSON.stringify({ detail })], { type: 'application/json' })
}

describe('extractErrorDetail', () => {
  it('取出已解析 JSON 响应里的 detail', async () => {
    await expect(extractErrorDetail(axiosError({ detail: '设备不存在' }))).resolves.toBe('设备不存在')
  })

  it('关键场景：responseType=blob 时错误体是 Blob，必须读成文本再解析', async () => {
    // 这正是 WinRM 脚本下载把「IPv6 目标」说成「请配置有效的虚拟机 IP」的原因：
    // axios 不解析 blob 错误体，读 .detail 永远是 undefined。
    const err = axiosError(jsonBlob('目标 2001:db8::10 是 IPv6，当前 WinRM 启用脚本仅支持 IPv4'))
    await expect(extractErrorDetail(err)).resolves.toBe('目标 2001:db8::10 是 IPv6，当前 WinRM 启用脚本仅支持 IPv4')
  })

  it('text/plain 的 Blob 错误体也能读出', async () => {
    const err = axiosError(new Blob(['plain failure'], { type: 'text/plain' }))
    await expect(extractErrorDetail(err)).resolves.toBe('plain failure')
  })

  it('二进制 Blob 不当作错误文案展示', async () => {
    // SFTP 下载失败时 data 可能是文件片段，展示出来是乱码
    const err = axiosError(new Blob([new Uint8Array([0x89, 0x50, 0x4e])], { type: 'image/png' }))
    await expect(extractErrorDetail(err, '下载失败')).resolves.toBe('下载失败')
  })

  it('超长文本截断，避免把整页堆栈塞进 toast', async () => {
    const long = 'x'.repeat(500)
    const err = axiosError(new Blob([long], { type: 'text/plain' }))
    const out = await extractErrorDetail(err)
    expect(out.length).toBeLessThanOrEqual(201)
    expect(out.endsWith('…')).toBe(true)
  })

  it('Blob 里不是合法 JSON 时退回原文', async () => {
    const err = axiosError(new Blob(['not json at all'], { type: 'application/json' }))
    await expect(extractErrorDetail(err)).resolves.toBe('not json at all')
  })

  it('detail 为空串时用兜底文案', async () => {
    await expect(extractErrorDetail(axiosError({ detail: '' }), '脚本生成失败')).resolves.toBe('脚本生成失败')
  })

  it('没有 response（网络断开/超时）时退回 message', async () => {
    await expect(extractErrorDetail({ message: 'Network Error' })).resolves.toBe('Network Error')
  })

  it('既无 response 也无 message 时用兜底', async () => {
    await expect(extractErrorDetail({}, '出错了')).resolves.toBe('出错了')
    await expect(extractErrorDetail(null, '出错了')).resolves.toBe('出错了')
    await expect(extractErrorDetail(undefined, '出错了')).resolves.toBe('出错了')
  })

  it('字符串形式的错误体也能解析出 detail', async () => {
    await expect(extractErrorDetail(axiosError('{"detail":"端口越界"}'))).resolves.toBe('端口越界')
  })
})

describe('readHeader', () => {
  it('大小写不敏感取值', () => {
    const headers = { 'x-dcn-winrm-source-ip': '192.168.1.10' }
    expect(readHeader(headers, 'X-DCN-WinRM-Source-IP')).toBe('192.168.1.10')
    expect(readHeader(headers, 'x-dcn-winrm-source-ip')).toBe('192.168.1.10')
  })

  it('缺失或类型不对时返回 undefined', () => {
    expect(readHeader({}, 'X-DCN-WinRM-Source-IP')).toBeUndefined()
    expect(readHeader(null, 'X-DCN-WinRM-Source-IP')).toBeUndefined()
    expect(readHeader(undefined, 'X-DCN-WinRM-Source-IP')).toBeUndefined()
    expect(readHeader({ 'x-dcn-winrm-source-ip': 42 as unknown as string }, 'X-DCN-WinRM-Source-IP')).toBeUndefined()
  })

  it('从错误对象的响应里读头', () => {
    const err = axiosError({}, { 'X-DCN-WinRM-Script-Version': '6' })
    expect(readResponseHeader(err, 'x-dcn-winrm-script-version')).toBe('6')
    expect(readResponseHeader({}, 'x-dcn-winrm-script-version')).toBeUndefined()
  })
})

describe('extractErrorDetail — 校验错误信封', () => {
  it('全局校验处理器返回的 message 必须透出（创建用户失败的真因）', async () => {
    const err = axiosError({
      success: false,
      code: 'VALIDATION_ERROR',
      message:
        '请求参数验证失败: body.password: Value error, 密码必须包含大写字母、小写字母、数字、特殊字符中的至少3种',
      traceId: 'abc',
    })
    const out = await extractErrorDetail(err, '创建用户失败')
    expect(out).toContain('密码必须包含')
    expect(out).not.toBe('创建用户失败')
  })

  it('FastAPI 原生 RequestValidationError 的 detail 是数组，取各项 msg', async () => {
    const err = axiosError({
      detail: [
        { loc: ['body', 'password'], msg: 'String should have at least 8 characters' },
        { loc: ['body', 'username'], msg: 'Field required' },
      ],
    })
    await expect(extractErrorDetail(err)).resolves.toBe('String should have at least 8 characters；Field required')
  })

  it('detail 优先于 message（HTTPException 的文案更具体）', async () => {
    const err = axiosError({ detail: '用户名已存在', message: '请求参数验证失败' })
    await expect(extractErrorDetail(err)).resolves.toBe('用户名已存在')
  })

  it('Blob 里的校验信封同样能解析', async () => {
    const blob = new Blob([JSON.stringify({ code: 'VALIDATION_ERROR', message: '密码长度不能少于8个字符' })], {
      type: 'application/json',
    })
    await expect(extractErrorDetail(axiosError(blob))).resolves.toBe('密码长度不能少于8个字符')
  })
})
