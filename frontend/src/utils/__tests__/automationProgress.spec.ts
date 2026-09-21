import { describe, expect, it } from 'vitest'
import { TARGET_FINAL_STATUSES, jobProgress, runningTargetProgress, targetsProgress } from '../automationProgress'
import type { AutomationJobListItem } from '../../types/automation'

/** 只有计数相关字段参与进度计算，其余字段给默认值，避免每个用例都抄一遍 */
function makeJob(overrides: Partial<AutomationJobListItem> = {}): AutomationJobListItem {
  return {
    id: 1,
    name: '健康巡检',
    job_type: 'inspection',
    trigger_type: 'manual',
    status: 'running',
    risk_level: 'low',
    created_by_name: 'admin',
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    finished_at: null,
    target_total: 4,
    succeeded: 0,
    warning: 0,
    failed: 0,
    running: 4,
    ...overrides,
  }
}

describe('TARGET_FINAL_STATUSES', () => {
  it('把 warning 当作终态', () => {
    // 后端把「巡检跑完但有异常」的 target 写成 warning，漏掉它进度就永远卡在 0%
    expect(TARGET_FINAL_STATUSES).toEqual(['completed', 'warning', 'failed'])
  })

  it('不包含运行中 / 等待中', () => {
    expect(TARGET_FINAL_STATUSES).not.toContain('running')
    expect(TARGET_FINAL_STATUSES).not.toContain('pending')
  })
})

describe('jobProgress', () => {
  it('回归：全部目标都是 warning 时必须算出 100%', () => {
    // 线上现象就是这一条：巡检完成但发现异常，后端 succeeded/failed 都是 0，
    // 旧实现 (succeeded + failed) / target_total 永远显示 0%。
    expect(jobProgress(makeJob({ target_total: 4, warning: 4, running: 0 }))).toBe(100)
  })

  it('单台设备巡检出异常也是 100%', () => {
    expect(jobProgress(makeJob({ target_total: 1, warning: 1, running: 0 }))).toBe(100)
  })

  it('混合终态按比例计算', () => {
    expect(jobProgress(makeJob({ target_total: 4, succeeded: 1, warning: 1, failed: 1, running: 1 }))).toBe(75)
  })

  it('全部成功仍是 100%，warning 计入没有改变原有口径', () => {
    expect(jobProgress(makeJob({ target_total: 3, succeeded: 3, running: 0 }))).toBe(100)
  })

  it('已启动但没有任何终态时显示 1%', () => {
    expect(jobProgress(makeJob({ target_total: 4, running: 4 }))).toBe(1)
  })

  it('Agent 任务:无终态但有步骤数时按步骤折算,列表进度实时爬升', () => {
    // 线上现象:列表 1% → 100% 跳变,因为 jobProgress 看不到步骤。
    // steps_executed 来自后端对运行中目标的步骤数聚合(增量落库)。
    const step1 = jobProgress(makeJob({ target_total: 1, running: 1, steps_executed: 1 }))
    const step5 = jobProgress(makeJob({ target_total: 1, running: 1, steps_executed: 5 }))
    const step20 = jobProgress(makeJob({ target_total: 1, running: 1, steps_executed: 20 }))
    expect(step5).toBeGreaterThan(step1)
    expect(step20).toBeGreaterThan(step5)
    // 步骤再多也封顶 95%:最后一段留给报告生成,终态才 100%
    expect(jobProgress(makeJob({ target_total: 1, running: 1, steps_executed: 50 }))).toBe(95)
    // 有终态后立即恢复目标级口径
    expect(jobProgress(makeJob({ target_total: 1, succeeded: 1, steps_executed: 20 }))).toBe(100)
  })

  it('steps_executed 缺省(非 agent 任务/旧接口)时回到 1% 下限,不算 NaN', () => {
    const legacy = { ...makeJob({ target_total: 1, running: 1 }), steps_executed: undefined }
    expect(jobProgress(legacy as unknown as AutomationJobListItem)).toBe(1)
  })

  it('还没启动（pending）时是 0%', () => {
    expect(jobProgress(makeJob({ target_total: 4, running: 0 }))).toBe(0)
  })

  it('target_total 为 0 时不炸', () => {
    expect(jobProgress(makeJob({ target_total: 0, running: 0 }))).toBe(0)
    expect(jobProgress(makeJob({ target_total: 0, running: 1 }))).toBe(1)
  })

  it('接口尚未返回 warning 字段时按 0 处理，不能算出 NaN', () => {
    // 后端字段是分批上线的，滚动升级期间老接口没有 warning
    const legacyJob = { ...makeJob({ target_total: 2, succeeded: 2, running: 0 }), warning: undefined }
    expect(jobProgress(legacyJob as unknown as AutomationJobListItem)).toBe(100)
  })

  it('计数溢出 target_total 时按四舍五入返回，不抛异常', () => {
    expect(jobProgress(makeJob({ target_total: 2, succeeded: 1, warning: 1, failed: 1, running: 0 }))).toBe(150)
  })
})

describe('targetsProgress', () => {
  it('空目标列表是 0%', () => {
    expect(targetsProgress([])).toBe(0)
  })

  it('warning 目标算已完成', () => {
    expect(targetsProgress([{ status: 'warning' }])).toBe(100)
  })

  it('终态与运行中混合:运行中按步骤折算贡献部分进度', () => {
    // 3 终态 + 1 运行中(已跑 5 步 ≈ 63%) = (3+0.63)/4 ≈ 91%
    const targets = [
      { status: 'completed' },
      { status: 'warning' },
      { status: 'failed' },
      {
        status: 'running',
        steps: [{ status: 'done' }, { status: 'done' }, { status: 'done' }, { status: 'done' }, { status: 'done' }],
      },
    ]
    expect(targetsProgress(targets)).toBe(Math.round(((3 + runningTargetProgress(targets[3].steps!) / 100) / 4) * 100))
  })

  it('无步骤数据的运行中目标:与列表页一致的 1% 下限', () => {
    expect(targetsProgress([{ status: 'running' }])).toBe(1)
    expect(targetsProgress([{ status: 'pending' }, { status: 'pending' }])).toBe(0)
  })
})

describe('runningTargetProgress(Agent 诊断步骤折算)', () => {
  it('步骤越多进度越高,单调递增', () => {
    const values = [1, 3, 5, 10, 15].map((n) =>
      runningTargetProgress(Array.from({ length: n }, () => ({ status: 'done' }))),
    )
    for (let i = 1; i < values.length; i++) expect(values[i]).toBeGreaterThan(values[i - 1])
  })

  it('步骤很多时封顶 95%,不提前显示完成', () => {
    // 进度满了任务还没完(LLM 还在写报告)是误导,最后 5% 留给终态
    const many = runningTargetProgress(Array.from({ length: 50 }, () => ({ status: 'done' })))
    expect(many).toBe(95)
  })

  it('无步骤为 0(由 targetsProgress 的 1% 下限兜底)', () => {
    expect(runningTargetProgress([])).toBe(0)
  })
})

describe('列表页与详情页口径一致', () => {
  it('同样的 1 成功 / 1 异常 / 1 失败，两处都是 100%', () => {
    const job = makeJob({ target_total: 3, succeeded: 1, warning: 1, failed: 1, running: 0 })
    const targets = [{ status: 'completed' }, { status: 'warning' }, { status: 'failed' }]
    expect(jobProgress(job)).toBe(100)
    expect(targetsProgress(targets)).toBe(100)
  })

  it('全部目标在跑且还没落任何步骤时,两处都显示 1%', () => {
    // 线上现象:列表 1%、详情 10%(0.1 个目标借位折算),用户不知道信哪个。
    const job = makeJob({ target_total: 1, running: 1 })
    const targets = [{ status: 'running' }]
    expect(jobProgress(job)).toBe(1)
    expect(targetsProgress(targets)).toBe(1)
  })

  it('单目标 Agent 诊断:步骤落库后详情进度随轮询爬升,列表在下轮刷新前仍 1%', () => {
    // 列表页只有目标级聚合数,看不到步骤;详情页轮询 1.5s 拿 steps 实时爬。
    // 二者短暂不一致是数据粒度差,不算口径矛盾:列表在下一轮刷新追平。
    const job = makeJob({ target_total: 1, running: 1 })
    const targets = [{ status: 'running', steps: Array.from({ length: 8 }, () => ({ status: 'done' })) }]
    expect(jobProgress(job)).toBe(1)
    expect(targetsProgress(targets)).toBeGreaterThan(1)
    expect(targetsProgress(targets)).toBeLessThan(100)
  })
})
