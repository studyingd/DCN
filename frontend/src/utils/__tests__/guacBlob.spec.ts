import { describe, expect, it } from 'vitest'
import { GUAC_B64_LENGTH, GUAC_BLOB_LENGTH, GUAC_READ_BLOCK, encodeBlobPieces } from '../guacBlob'

/** 逐块编码的参考实现（等价于 guacamole-common-js 的 __send_blob）。 */
function referenceEncode(bytes: Uint8Array): string[] {
  const out: string[] = []
  for (let i = 0; i < bytes.length; i += GUAC_BLOB_LENGTH) {
    const chunk = bytes.subarray(i, i + GUAC_BLOB_LENGTH)
    let binary = ''
    for (let j = 0; j < chunk.length; j++) binary += String.fromCharCode(chunk[j])
    out.push(btoa(binary))
  }
  return out
}

function bytes(n: number, seed = 1): Uint8Array {
  const out = new Uint8Array(n)
  let x = seed
  for (let i = 0; i < n; i++) {
    // 覆盖全字节域，包含 0x00 与 0xff 这类边界值
    x = (x * 1103515245 + 12345) & 0x7fffffff
    out[i] = x % 256
  }
  return out
}

describe('encodeBlobPieces', () => {
  it('块长必须是 3 的整数倍，否则整块 base64 会插入 padding 导致切分错位', () => {
    expect(GUAC_BLOB_LENGTH % 3).toBe(0)
    expect(GUAC_B64_LENGTH).toBe((GUAC_BLOB_LENGTH / 3) * 4)
    expect(GUAC_READ_BLOCK % GUAC_BLOB_LENGTH).toBe(0)
  })

  it('单条 base64 载荷不超过 guacd 8192 字节的指令上限', () => {
    for (const piece of encodeBlobPieces(bytes(GUAC_READ_BLOCK))) {
      expect(piece.length).toBeLessThanOrEqual(GUAC_B64_LENGTH)
    }
  })

  it('快路径与逐块编码完全等价（整数倍长度）', () => {
    for (const n of [GUAC_BLOB_LENGTH, GUAC_BLOB_LENGTH * 2, GUAC_READ_BLOCK]) {
      const data = bytes(n)
      expect(encodeBlobPieces(data)).toEqual(referenceEncode(data))
    }
  })

  it('尾部不足一块时也等价，且最后一块自带 padding', () => {
    for (const n of [1, 3, 6047, GUAC_BLOB_LENGTH + 1, GUAC_READ_BLOCK + 777]) {
      const data = bytes(n)
      const pieces = encodeBlobPieces(data)
      expect(pieces).toEqual(referenceEncode(data))
      // 还原后必须与原始字节逐字节相同
      const joined = pieces.map((p) => atob(p)).join('')
      expect(joined.length).toBe(n)
      for (let i = 0; i < n; i++) expect(joined.charCodeAt(i)).toBe(data[i])
    }
  })

  it('空输入返回空数组', () => {
    expect(encodeBlobPieces(new Uint8Array(0))).toEqual([])
  })
})
