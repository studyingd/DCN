/**
 * 从接口错误里取出后端真正的 `detail` 文案。
 *
 * 为什么需要这个：axios 对 `responseType: 'blob'` 的请求**不会解析错误体**。
 * 后端返回 4xx/5xx 时 `err.response.data` 是一个 Blob，里面装着
 * `{"detail": "..."}`，直接读 `.detail` 永远是 undefined。于是各处只能写一句
 * 笼统的兜底文案——WinRM 脚本下载就因此把「IPv6 不支持」「无法确定出口 IP」
 * 这类精确原因统统说成「请先配置有效的虚拟机 IP」，把人往错误的方向引。
 */

/** axios 错误对象里我们用到的最小形状 */
interface AxiosLikeError {
  isAxiosError?: boolean
  response?: {
    status?: number
    data?: unknown
    headers?: Record<string, string>
  }
  message?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/**
 * 后端错误体有两种形态，都要认：
 * - `HTTPException` → `{"detail": "..."}`
 * - 全局校验/异常处理器 → `{"success": false, "code": "VALIDATION_ERROR", "message": "..."}`
 * 只读 `detail` 会让所有 422（密码强度、字段越界等）在前端退化成笼统文案。
 */
function pickErrorText(value: unknown): string | undefined {
  if (!isRecord(value)) return undefined
  for (const key of ['detail', 'message'] as const) {
    const field = value[key]
    if (typeof field === 'string' && field.trim()) return field
    // FastAPI 原生 RequestValidationError 的 detail 是数组
    if (Array.isArray(field)) {
      const parts = field
        .map((item) => (isRecord(item) && typeof item.msg === 'string' ? item.msg : ''))
        .filter(Boolean)
      if (parts.length) return parts.join('；')
    }
  }
  return undefined
}

/**
 * 解析错误响应体，兼容三种形态：
 * 已解析对象 / JSON 文本 / Blob（`responseType: 'blob'` 的错误路径）。
 *
 * Blob 只能异步读取，因此本函数返回 Promise。
 */
export async function extractErrorDetail(error: unknown, fallback = '请求失败'): Promise<string> {
  const err = error as AxiosLikeError
  const data = err?.response?.data

  if (data == null) return err?.message || fallback

  // 1) 普通 JSON 请求：axios 已解析
  const direct = pickErrorText(data)
  if (direct) return direct

  // 2) Blob：需要读成文本再解析
  if (typeof Blob !== 'undefined' && data instanceof Blob) {
    // 二进制文件（SFTP 下载等）失败时，不要把文件内容当错误文案展示
    const isTextual = data.type.includes('json') || data.type.includes('text')
    if (data.type && !isTextual) return fallback
    try {
      const text = (await data.text()).trim()
      if (!text) return fallback
      // JSON 解析失败不能直接落到兵底：FastAPI 的 HTTPException 给 JSON，
      // 但 PlainTextResponse 给纯文本，两种都是真实的错误文案。
      try {
        const parsed: unknown = JSON.parse(text)
        const fromBlob = pickErrorText(parsed)
        if (fromBlob) return fromBlob
      } catch {
        // 不是 JSON，继续按原文展示
      }
      // 不是 JSON 结构就把原文截断展示，至少比笼统文案有用
      return text.length > 200 ? `${text.slice(0, 200)}…` : text
    } catch {
      // 只有读 Blob 本身失败（如已被消费）才用兜底
      return fallback
    }
  }

  // 3) 纯文本响应
  if (typeof data === 'string' && data.trim()) {
    try {
      const parsed: unknown = JSON.parse(data)
      const fromText = pickErrorText(parsed)
      if (fromText) return fromText
    } catch {
      // 不是 JSON，原样返回
    }
    return data.length > 200 ? `${data.slice(0, 200)}…` : data
  }

  return fallback
}

/** 在任意 headers 映射里大小写不敏感地取值（axios 会归一化，但代理/降级路径不保证） */
export function readHeader(headers: Record<string, unknown> | undefined | null, name: string): string | undefined {
  if (!isRecord(headers)) return undefined
  const lower = name.toLowerCase()
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() === lower && typeof value === 'string') return value
  }
  return undefined
}

/** 从错误对象的响应里读头 */
export function readResponseHeader(error: unknown, name: string): string | undefined {
  return readHeader((error as AxiosLikeError)?.response?.headers, name)
}
