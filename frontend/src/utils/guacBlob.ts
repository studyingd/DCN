/**
 * GuacamoleFS 上传的分块编码工具。
 *
 * 为什么必须自己分块：guacd 限制单条指令最长 8192 字节，blob 的内容是 base64，
 * 所以原始数据每块最多 6048 字节（base64 后 8064 字符）。guacamole-common-js 的
 * `BlobWriter` 就是这个尺寸，但它是**严格一发一收**——每发一块都要等 guacd 的
 * ack，而且每块都单独走一次 `FileReader.readAsArrayBuffer`。上传 100MB 就是
 * 一万七千多个「异步读 + 一个 RTT」，慢得离谱。
 *
 * 这里提供一个纯函数把整块字节一次性编码成多条 blob 载荷，配合调用方的滑动窗口
 * （多块在途、按 ack 补发）就能把 RTT 摊薄几十倍。
 *
 * 等价性保证：6048 是 3 的整数倍，base64 每 3 字节编成 4 字符且不产生 padding，
 * 因此「整块 btoa 后按 8064 字符切分」与「逐块 btoa」结果完全一致。
 */

/** 单条 blob 指令能承载的原始字节数（guacd 指令上限 8192，base64 后 8064）。 */
export const GUAC_BLOB_LENGTH = 6048

/** 单块原始数据 base64 编码后的字符数。 */
export const GUAC_B64_LENGTH = 8064

/**
 * 一次异步读取的字节数。取 GUAC_BLOB_LENGTH 的整数倍，保证整块编码后
 * 能按 GUAC_B64_LENGTH 精确切分（不会跨块产生 padding）。约 254KB。
 */
export const GUAC_READ_BLOCK = GUAC_BLOB_LENGTH * 43

function toBinaryString(bytes: Uint8Array): string {
  // 分段 apply，避免一次性展开几十万个实参导致调用栈溢出。
  let binary = ''
  const STEP = 32768
  for (let i = 0; i < bytes.length; i += STEP) {
    const part = bytes.subarray(i, i + STEP)
    binary += String.fromCharCode.apply(null, Array.from(part) as unknown as number[])
  }
  return binary
}

/**
 * 把一段字节编码成若干条可直接交给 `stream.sendBlob()` 的 base64 载荷。
 *
 * 长度是 GUAC_BLOB_LENGTH 整数倍时走「整块编码 + 定长切分」的快路径；
 * 否则逐块编码，让最后一块自然带上 base64 padding。
 */
export function encodeBlobPieces(block: Uint8Array): string[] {
  if (!block.length) return []

  if (block.length % GUAC_BLOB_LENGTH === 0) {
    const b64 = btoa(toBinaryString(block))
    const pieces: string[] = []
    for (let i = 0; i < b64.length; i += GUAC_B64_LENGTH) {
      pieces.push(b64.slice(i, i + GUAC_B64_LENGTH))
    }
    return pieces
  }

  const pieces: string[] = []
  for (let i = 0; i < block.length; i += GUAC_BLOB_LENGTH) {
    pieces.push(btoa(toBinaryString(block.subarray(i, i + GUAC_BLOB_LENGTH))))
  }
  return pieces
}
