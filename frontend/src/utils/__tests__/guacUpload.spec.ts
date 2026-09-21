import { describe, expect, it, vi } from 'vitest'
import { GUAC_BLOB_LENGTH, GUAC_READ_BLOCK } from '../guacBlob'
import { uploadBlobPipelined, type GuacAckStatus, type GuacStreamLike, type UploadPhase } from '../guacUpload'

/** 可编程的假输出流：记录发出的 blob，并按脚本回 ack。 */
class FakeStream implements GuacStreamLike {
  onack: ((status: GuacAckStatus) => void) | null = null
  blobs: string[] = []
  ended = false
  /** 每收到多少条 blob 自动回一次 ack；0 表示完全不回（模拟远端卡死）。 */
  autoAckEvery = 1

  sendBlob(data: string) {
    this.blobs.push(data)
    if (this.autoAckEvery > 0 && this.blobs.length % this.autoAckEvery === 0) {
      this.ack()
    }
  }
  sendEnd() {
    this.ended = true
  }
  ack(message?: string) {
    this.onack?.({ isError: () => !!message, message })
  }
}

function makeFile(size: number): Blob {
  const bytes = new Uint8Array(size)
  for (let i = 0; i < size; i++) bytes[i] = (i * 31 + 7) % 256
  return new Blob([bytes])
}

/** 把 base64 载荷还原成字节，用于校验收到的内容与原文件一致。 */
function decodePieces(pieces: string[]): Uint8Array {
  const out: number[] = []
  for (const piece of pieces) {
    const binary = atob(piece)
    for (let i = 0; i < binary.length; i++) out.push(binary.charCodeAt(i))
  }
  return new Uint8Array(out)
}

describe('uploadBlobPipelined', () => {
  it('完整发出全部内容并 sendEnd，字节与原文件一致', async () => {
    const size = GUAC_READ_BLOCK * 2 + 1234
    const file = makeFile(size)
    const stream = new FakeStream()

    const written = await uploadBlobPipelined(file, stream, { window: 4 })

    expect(written).toBe(size)
    expect(stream.ended).toBe(true)
    const original = new Uint8Array(await file.arrayBuffer())
    expect(decodePieces(stream.blobs)).toEqual(original)
  })

  it('单条 blob 不超过 guacd 的指令上限', async () => {
    const stream = new FakeStream()
    await uploadBlobPipelined(makeFile(GUAC_READ_BLOCK + 5), stream, { window: 2 })
    for (const piece of stream.blobs) {
      // 4.blob,<index>,<payload>; 加上长度前缀后仍必须 < 8192
      expect(piece.length).toBeLessThanOrEqual(8064)
    }
  })

  it('遵守在途窗口：未确认的 blob 数从不超过 window', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0 // 完全不回 ack
    let maxOutstanding = 0
    let outstanding = 0
    const rawSend = stream.sendBlob.bind(stream)
    stream.sendBlob = (data: string) => {
      outstanding++
      maxOutstanding = Math.max(maxOutstanding, outstanding)
      rawSend(data)
    }

    const promise = uploadBlobPipelined(makeFile(GUAC_READ_BLOCK * 4), stream, {
      window: 3,
      stallTimeoutMs: 500,
    })
    // 让 pump 跑完第一轮
    await new Promise((r) => setTimeout(r, 10))
    expect(maxOutstanding).toBe(3)

    // 补 ack 让它继续推进，避免留下未处理的 rejection。
    // pump 内 refill() 读文件是宏任务,给宏任务一点推进时间(而非纯微任务)。
    for (let i = 0; i < 400 && !stream.ended; i++) {
      outstanding = Math.max(0, outstanding - 1)
      stream.ack()
      await new Promise((r) => setTimeout(r, 0))
    }
    await expect(promise).resolves.toBeGreaterThan(0)
  })

  it('远端长时间不 ack 时按超时失败，而不是永久挂起', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0
    await expect(
      uploadBlobPipelined(makeFile(GUAC_READ_BLOCK), stream, { window: 2, stallTimeoutMs: 30 }),
    ).rejects.toThrow('上传超时')
  })

  it('远端返回错误 ack 时立即失败', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0
    const promise = uploadBlobPipelined(makeFile(GUAC_READ_BLOCK * 2), stream, {
      window: 2,
      stallTimeoutMs: 5000,
    })
    stream.onack?.({ isError: () => true, message: '磁盘空间不足' })
    await expect(promise).rejects.toThrow('磁盘空间不足')
  })

  it('进度按已确认字节推进，sending 阶段不超过 99%', async () => {
    const size = GUAC_READ_BLOCK * 2
    const stream = new FakeStream()
    const seen: { pct: number; phase: UploadPhase }[] = []

    await uploadBlobPipelined(makeFile(size), stream, {
      window: 4,
      onProgress: (pct, phase) => seen.push({ pct, phase }),
    })

    const sending = seen.filter((s) => s.phase === 'sending')
    expect(sending.length).toBeGreaterThan(0)
    expect(sending.every((s) => s.pct <= 99)).toBe(true)
    // 单调不回退
    for (let i = 1; i < sending.length; i++) {
      expect(sending[i].pct).toBeGreaterThanOrEqual(sending[i - 1].pct)
    }
    expect(seen[seen.length - 1]).toEqual({ pct: 100, phase: 'flushing' })
  })

  it('全部字节交出后进入 flushing 阶段', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0
    const phases: UploadPhase[] = []
    const promise = uploadBlobPipelined(makeFile(GUAC_BLOB_LENGTH * 3), stream, {
      window: 8,
      stallTimeoutMs: 5000,
      onProgress: (_p, phase) => phases.push(phase),
    })
    await vi.waitFor(() => expect(phases).toContain('flushing'))
    // 补齐 ack 收尾
    for (let i = 0; i < stream.blobs.length; i++) stream.ack()
    await expect(promise).resolves.toBe(GUAC_BLOB_LENGTH * 3)
  })

  it('空文件直接结束，不发 blob', async () => {
    const stream = new FakeStream()
    await expect(uploadBlobPipelined(makeFile(0), stream)).resolves.toBe(0)
    expect(stream.blobs).toEqual([])
    expect(stream.ended).toBe(true)
  })

  it('window=1 时退化为逐条串行（与旧 BlobWriter 行为一致）', async () => {
    const stream = new FakeStream()
    const written = await uploadBlobPipelined(makeFile(GUAC_BLOB_LENGTH * 5), stream, { window: 1 })
    expect(written).toBe(GUAC_BLOB_LENGTH * 5)
    expect(stream.blobs.length).toBe(5)
    expect(stream.ended).toBe(true)
  })
})

describe('uploadBlobPipelined — 进度节流', () => {
  it('每条 ack 都回调会拖慢上传，因此同一百分比在间隔内只上报一次', async () => {
    const size = GUAC_READ_BLOCK * 3
    const stream = new FakeStream()
    const reports: { pct: number; phase: UploadPhase }[] = []

    await uploadBlobPipelined(makeFile(size), stream, {
      window: 8,
      onProgress: (pct, phase) => reports.push({ pct, phase }),
    })

    const blobCount = stream.blobs.length
    expect(blobCount).toBeGreaterThan(reports.length)
    // 百分比不允许回退
    const pcts = reports.map((r) => r.pct)
    expect(pcts).toEqual([...pcts].sort((a, b) => a - b))
    // 收尾一定是 100%
    expect(reports[reports.length - 1]).toEqual({ pct: 100, phase: 'flushing' })
  })

  it('阶段切换到 flushing 必须立即上报，不受节流影响', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0
    const phases: UploadPhase[] = []
    const promise = uploadBlobPipelined(makeFile(GUAC_BLOB_LENGTH * 3), stream, {
      window: 8,
      stallTimeoutMs: 5000,
      onProgress: (_p, phase) => phases.push(phase),
    })
    await vi.waitFor(() => expect(phases[phases.length - 1]).toBe('flushing'))
    for (let i = 0; i < stream.blobs.length; i++) stream.ack()
    await expect(promise).resolves.toBe(GUAC_BLOB_LENGTH * 3)
  })
})

describe('uploadBlobPipelined — 看门狗分阶段', () => {
  it('尾 ack 未回齐（数据已读完）时用独立限额，不被传输阶段超时误杀', async () => {
    const stream = new FakeStream()
    // 远端回 ack 但很慢：模拟最后一个窗口的确认迟迟不到
    stream.autoAckEvery = 0
    const started = Date.now()

    // 文件只有 3 条 blob、窗口 8：全部发出后数据即读完(ended)，但尾 ack 未回，
    // 进入「等尾 ack」阶段。stallTimeoutMs=40ms 若被沿用就会误报超时。
    const promise = uploadBlobPipelined(makeFile(GUAC_BLOB_LENGTH * 3), stream, {
      window: 8,
      stallTimeoutMs: 150,
      flushTimeoutMs: 2000,
    })
    // 数据很快读完 → ended 阶段开始(但还没 sendEnd,因为 ack 未齐)
    await new Promise((r) => setTimeout(r, 30))

    // 静默 400ms(远大于 stallTimeoutMs、远小于 flushTimeoutMs):不应失败
    await new Promise((r) => setTimeout(r, 400))
    await expect(Promise.race([promise, Promise.resolve('pending')])).resolves.toBe('pending')
    expect(Date.now() - started).toBeLessThan(1000)

    // 远端最终确认全部 blob → ack 回齐后才 sendEnd → 正常完成
    for (let i = 0; i < stream.blobs.length; i++) stream.ack()
    await expect(promise).resolves.toBe(GUAC_BLOB_LENGTH * 3)
    expect(stream.ended).toBe(true)
  })

  it('尾 ack 迟迟不回时，报的是"未完成写入"而不是笼统超时', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0 // 回 0 条:数据读完但尾 ack 永远不到
    await expect(
      uploadBlobPipelined(makeFile(GUAC_BLOB_LENGTH * 3), stream, {
        window: 8,
        stallTimeoutMs: 20,
        flushTimeoutMs: 60,
      }),
    ).rejects.toThrow('未完成写入')
  })

  it('传输阶段（尚未 sendEnd）超时仍报"未确认数据"', async () => {
    const stream = new FakeStream()
    stream.autoAckEvery = 0
    // 文件远大于窗口，所以会停在传输阶段而不是落盘阶段
    await expect(
      uploadBlobPipelined(makeFile(GUAC_READ_BLOCK * 3), stream, {
        window: 2,
        stallTimeoutMs: 30,
        flushTimeoutMs: 5000,
      }),
    ).rejects.toThrow('未确认数据')
  })
})
