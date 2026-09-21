import type { AutomationJobListItem } from '@/types/automation'

/**
 * 目标（AutomationTarget）的终态集合。
 *
 * `warning` 表示「巡检已经跑完，但发现了异常项」：后端 automation.py 把巡检的
 * partial 结果映射成 target.status = "warning"。它既不算成功也不算失败，但一定
 * 已经结束。任何进度口径漏掉它，都会让「巡检完成但有异常」的任务永远停在 0%。
 */
export const TARGET_FINAL_STATUSES: readonly string[] = ['completed', 'warning', 'failed']

/**
 * Agent 诊断的运行中目标按「已执行步骤数」折算进度。
 *
 * 单目标 Agent 任务曾长期停在 1%(终态粒度太粗,LLM 一轮几十秒),看起来
 * 像卡死。步骤经 on_step 每 1.5s 落库,详情抽屉轮询即可看到进度从 1% 平滑
 * 爬升。步骤没有「总数」上限(LLM 自主决定取证多少项),所以按渐进收敛:
 * 前几步爬得快,越往后每步增益越小,封顶 95%——最后 5% 留给报告生成,
 * 真实完成(终态)时才到 100%,避免「进度满了任务还没完」的误导。
 */
export function runningTargetProgress(steps: readonly { status: string }[]): number {
  const executed = steps.length
  if (executed === 0) return 0
  return Math.min(95, Math.round((1 - Math.pow(0.82, executed)) * 100))
}

/**
 * 任务列表行的进度百分比，数据来自后端聚合的 succeeded / failed / warning / running 计数。
 * Agent 任务额外消费 steps_executed(后端聚合的运行中目标累计步骤数):
 * 列表页与详情抽屉同一折算口径(runningTargetProgress),配合历史页轮询
 * 实时爬升,不再 1% → 100% 跳变。
 */
export function jobProgress(job: AutomationJobListItem): number {
  // warning 与 succeeded / failed 同属终态，必须一起计入分子，否则进度会卡在 0%。
  // `?? 0` 是滚动升级期的兜底：接口尚未返回 warning 字段时不至于算出 NaN。
  const finished = job.succeeded + job.failed + (job.warning ?? 0)
  const total = Math.max(job.target_total, 1)
  if (finished > 0) return Math.round((finished / total) * 100)
  if (job.running === 0) return 0
  // 没有任何终态:Agent 任务按累计步骤数折算(后端给的是运行中目标合计,
  // 单目标即自身进度;多目标任务按步骤平均爬升);巡检/脚本任务步骤粒度
  // 太粗没有折算价值,沿用 1% 下限。
  const steps = job.steps_executed ?? 0
  if (steps > 0) {
    return Math.min(95, Math.round((1 - Math.pow(0.82, steps)) * 100)) || 1
  }
  return 1
}

/**
 * 任务详情抽屉的进度百分比，直接按 target.status 统计。
 * 与 jobProgress 共用 TARGET_FINAL_STATUSES，保证列表页与详情页口径一致。
 * 入参取结构化最小形状(结构化类型兼容 AutomationTarget,测试无需造全字段)。
 */
export function targetsProgress(targets: readonly { status: string; steps?: readonly { status: string }[] }[]): number {
  if (targets.length === 0) return 0
  let sum = 0
  for (const target of targets) {
    if (TARGET_FINAL_STATUSES.includes(target.status)) {
      sum += 1
    } else if (target.status === 'running') {
      // 运行中目标按步骤推进折算(0~0.95),步骤数据随轮询实时更新
      sum += runningTargetProgress(target.steps || []) / 100
    }
  }
  const progress = Math.round((sum / targets.length) * 100)
  // 已启动但还没有任何终态、步骤也还没落库时,显示与列表页相同的 1% 下限
  // (表示「确实跑起来了」而不是真实进度——目标在跑时拿不到更细的粒度)。
  const running = targets.filter((t) => t.status === 'running').length
  if (progress === 0 && running > 0) return 1
  return progress
}
