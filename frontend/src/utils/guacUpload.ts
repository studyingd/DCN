/**
 * GuacamoleFS 上传的窗口化流水线状态机。
 *
 * 为什么不用 guacamole-common-js 的 `BlobWriter`：它严格一发一收（每 6048 字节
 * 等一次 ack），而且每块单独走一次 `FileReader` 异步读取。上传 100MB 就是一万七千
 * 多个「异步读 + 一个 RTT」，吞吐被往返次数而非带宽压死。
 *
 * 这里改成滑动窗口：一次读 ~254KB、整块编码后切出 43 条 blob，最多 `window` 条
 * 在途，收到 ack 再补发。抽成独立模块是为了能用假的 stream 把状态机测干净——
 * 闭包里握着 guacd 对象的版本没法单测，出问题时只能靠线上复现。
 */

import { GUAC_BLOB_LENGTH, GUAC_READ_BLOCK, encodeBlobPieces } from './guacBlob'

/** 上传阶段：sending 的百分比可信，flushing 表示字节已交出、等远端落盘。 */
export type UploadPhase = 'sending' | 'flushing'

/** guacamole-common-js OutputStream 里我们用到的最小形状，便于测试注入。 */
export interface GuacStreamLike {
  sendBlob(data: string): void
  sendEnd(): void
  onack: ((status: GuacAckStatus) => void) | null
}

export interface GuacAckStatus {
  isError?: () => boolean
  message?: string
}

export interface PipelineOptions {
  /** 在途 blob 上限。默认 16（约 97KB），远低于 guacd 缓冲。 */
  window?: number
  /** 传输途中连续这么久没收到任何 ack 就判定失败。 */
  stallTimeoutMs?: number
  /** 字节全部交出后，等待远端关流落盘的上限。 */
  flushTimeoutMs?: number
  onProgress?: (pct: number, phase: UploadPhase) => void
}

/**
 * 在途窗口。窗口决定吞吐上限：吞吐 ≈ window × 6048B / RTT。
 * 串行（window=1）在 20ms 链路上只有 ~300KB/s，128 条约 ~38MB/s。
 * 后端实测 guacd→Windows 落盘能跑 6.9MB/s（直连测试），瓶颈在浏览器→后端
 * 的逐条 socket.send 与 RTT。加大窗口把多个 RTT 折叠成一个，是直接有效的
 * 提速手段。128 × 6048B ≈ 774KB 在途，仍在 guacd 可接受的范围内。
 */
const DEFAULT_WINDOW = 128

/**
 * 这是"一个 ack 都没收到"的看门狗，不是吞吐要求。慢链路可能在传输途中
 * 短暂不回 ack，设得太短会把正在正常推进的上传误判成超时，所以给到 5 分钟；
 * 只要有 ack 就会重置。
 */
const DEFAULT_STALL_TIMEOUT_MS = 300_000

/**
 * 字节全部交出、`sendEnd` 之后的等待上限。
 *
 * 这一段必须单独给一个宽得多的限额：guacd 会把数据先收下并逐条 ack（所以进度
 * 很快到 99%），而真正推送给 Windows 共享盘发生在关流时，期间**没有任何 ack**。
 * 曾经用同一个看门狗覆盖这段静默期，结果大文件上传在落盘阶段被误报成
 * "上传超时"。落盘多久取决于 RDP 通道与文件大小，无法从客户端观测。
 */
const DEFAULT_FLUSH_TIMEOUT_MS = 30 * 60_000

/** 进度回调的最小间隔：够顺滑，又不会让每条 ack 都触发一次重渲染。 */
const PROGRESS_INTERVAL_MS = 120

/**
 * 把一个 Blob 通过输出流发到远端，返回字节数。
 *
 * 进度一律按**已被 ack 的字节**计算：guacd 收下数据就 ack，真正写进远程共享盘
 * 还在后面，用「已发出」会让进度条提前冲到 99% 然后长时间不动。字节全部交出后
 * 进入 'flushing' 阶段，由界面如实展示。
 */
export function uploadBlobPipelined(
  file: Blob,
  stream: GuacStreamLike,
  options: PipelineOptions = {},
): Promise<number> {
  const window_ = Math.max(1, options.window ?? DEFAULT_WINDOW)
  const stallTimeoutMs = options.stallTimeoutMs ?? DEFAULT_STALL_TIMEOUT_MS
  const flushTimeoutMs = options.flushTimeoutMs ?? DEFAULT_FLUSH_TIMEOUT_MS
  const onProgress = options.onProgress

  const total = file.size

  return new Promise<number>((resolve, reject) => {
    // 空文件：没有 blob 可发，直接结束流。
    if (!total) {
      stream.sendEnd()
      onProgress?.(100, 'flushing')
      resolve(0)
      return
    }

    let settled = false
    let sent = 0 // 已发出的 blob 条数
    let acked = 0 // 已确认的 blob 条数
    let ended = false
    let pumping = false
    let readOffset = 0
    let pieces: string[] = []
    let stallTimer: ReturnType<typeof setTimeout> | null = null
    let lastReportedPhase: UploadPhase = 'sending'
    let lastReportedPct = -1
    let lastReportedAt = 0

    const clearStall = () => {
      if (stallTimer) {
        clearTimeout(stallTimer)
        stallTimer = null
      }
    }
    const armStall = () => {
      clearStall()
      // 落盘阶段（已 sendEnd）用宽得多的限额，原因见 DEFAULT_FLUSH_TIMEOUT_MS。
      const limit = ended ? flushTimeoutMs : stallTimeoutMs
      stallTimer = setTimeout(() => {
        fail(
          new Error(
            ended ? '上传超时：远端长时间未完成写入，请检查 RDP 会话是否仍然存活' : '上传超时：远端长时间未确认数据',
          ),
        )
      }, limit)
    }
    function fail(err: unknown) {
      if (settled) return
      settled = true
      clearStall()
      reject(err instanceof Error ? err : new Error(String(err)))
    }
    function done() {
      if (settled) return
      settled = true
      clearStall()
      lastReportedPhase = 'flushing'
      lastReportedPct = 100
      onProgress?.(100, 'flushing')
      resolve(total)
    }
    /**
     * 进度只认已确认的字节；99% 留给 flush 阶段，避免"看起来完成了"。
     *
     * 必须节流：每条 blob 一个 ack，100MB 就是一万七千多次回调，而每次回调都会
     * 驱动 Vue 响应式与进度条重渲染——全都挤在处理 ack 的同一个 JS 线程上，
     * 反过来把上传本身拖慢。只在百分比真的变化、或超过 PROGRESS_INTERVAL_MS
     * 时才往外报；阶段切换（进入 flushing）总是立即上报。
     */
    function reportProgress(phase: UploadPhase, force = false) {
      if (!onProgress) return
      const bytesAcked = Math.min(acked * GUAC_BLOB_LENGTH, total)
      const pct = phase === 'flushing' ? 100 : Math.min(99, Math.round((bytesAcked / total) * 100))
      const now = Date.now()
      const phaseChanged = phase !== lastReportedPhase
      if (!force && !phaseChanged && pct === lastReportedPct && now - lastReportedAt < PROGRESS_INTERVAL_MS) {
        return
      }
      lastReportedPhase = phase
      lastReportedPct = pct
      lastReportedAt = now
      onProgress(pct, phase)
    }

    stream.onack = (status) => {
      if (settled) return
      if (status?.isError?.()) {
        fail(new Error(status.message || '上传失败'))
        return
      }
      acked++
      armStall()
      // 数据已读完(ended)且所有 blob 都已确认 → 现在才安全 sendEnd + done。
      // 必须先等 acked>=sent 再 sendEnd:否则 endStream 会删掉 output_streams[index],
      // 把还在路上的尾部 ack 全丢掉(见 pump 末尾注释)。
      if (ended && acked >= sent) {
        armStall() // 落盘阶段限额
        stream.sendEnd()
        reportProgress('flushing', true)
        done()
        return
      }
      reportProgress(ended ? 'flushing' : 'sending')
      pump().catch(fail)
    }

    /** 队列空且文件未读完时，再异步读一大块并编码。 */
    async function refill(): Promise<void> {
      if (pieces.length || readOffset >= total) return
      const end = Math.min(readOffset + GUAC_READ_BLOCK, total)
      const block = new Uint8Array(await file.slice(readOffset, end).arrayBuffer())
      readOffset = end
      pieces = encodeBlobPieces(block)
    }

    async function pump(): Promise<void> {
      // 单次只允许一个 pump 在跑；onack 触发的重入直接返回，由在跑的那个继续，
      // 因为 while 条件每轮都会重新读取最新的 acked。
      if (pumping || settled) return
      pumping = true
      try {
        // 每发 YIELD_EVERY 条就让出一次事件循环。否则一次 refill 读出的 43 条会
        // 在一个宏任务里连续同步 socket.send,把上行缓冲占满,导致下行 ack 的
        // onmessage 迟迟得不到处理——正是「最后一批 blob 的 ack 收不齐,卡死在
        // flushing」的根因。让出后浏览器才有机会处理 ack、推进窗口。
        let sinceYield = 0
        while (!settled && sent - acked < window_) {
          await refill()
          const piece = pieces.shift()
          if (piece === undefined) break
          sent++
          stream.sendBlob(piece)
          if (++sinceYield >= 16) {
            sinceYield = 0
            // 必须让出宏任务:浏览器 WebSocket 的 onmessage(ack)是宏任务,连续
            // 同步 socket.send 会占满当前宏任务,把下行 ack 饿死——这正是「最后
            // 一批 blob 的 ack 收不齐、卡死在 flushing」的根因。setTimeout(0)
            // 让 onmessage 有机会插入执行、推进窗口。
            await new Promise<void>((r) => setTimeout(r, 0))
          }
        }
        if (settled) return
        reportProgress(ended ? 'flushing' : 'sending')
        // 数据全部从文件读出后,**不能立即 sendEnd**。guacamole-common-js 的
        // endStream 会「发 end 指令 + 立即 delete output_streams[index]」,若此时
        // 最后一个窗口的 blob ack 还在路上,这些 ack 到达时流已被删 → onack 不
        // 再触发 → acked 永远到不了 sent → 卡死在 flushing(实测丢整整一个窗口
        // 的 ack)。正确顺序:先等 acked >= sent(所有 blob 都已确认),再 sendEnd。
        if (!ended && readOffset >= total && !pieces.length) {
          if (acked >= sent) {
            // 所有 blob 已确认,现在才安全 sendEnd
            ended = true
            armStall() // 落盘阶段限额
            stream.sendEnd()
            reportProgress('flushing', true)
            done()
          } else {
            // 还有 blob 未确认:切到等尾 ack 阶段。此时 ended=true,看门狗必须
            // 换用 flushTimeoutMs(尾 ack 可能迟迟才到),否则会被 stallTimeoutMs
            // 误杀。armStall() 内部按 ended 选限额,所以要在 ended=true 之后调。
            ended = true // 用 ended 表示「数据已读完」,但 sendEnd 推迟
            armStall() // 切换到落盘/尾 ack 阶段的宽限额
            reportProgress('flushing', true)
          }
        }
      } finally {
        pumping = false
      }
    }

    armStall()
    pump().catch(fail)
  })
}
