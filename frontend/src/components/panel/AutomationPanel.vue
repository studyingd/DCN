<template>
  <div class="inline-panel automation-center">
    <div class="inline-panel-header automation-header">
      <div>
        <h2 class="inline-panel-title">自动化运维</h2>
      </div>
      <el-button type="primary" @click="openCreate()">
        <el-icon><Plus /></el-icon>新建任务
      </el-button>
    </div>

    <div class="automation-body">
      <div class="automation-tabs" role="tablist" aria-label="自动化运维导航">
        <button
          v-for="item in sections"
          :key="item.key"
          type="button"
          :class="{ active: section === item.key }"
          @click="section = item.key"
        >
          <el-icon><component :is="item.icon" /></el-icon>{{ item.label }}
        </button>
      </div>

      <section v-if="section === 'overview'" class="automation-page">
        <div class="summary-grid">
          <button
            v-for="item in summaryCards"
            :key="item.status"
            type="button"
            class="summary-card"
            @click="openHistory(item.status)"
          >
            <span class="summary-icon" :class="item.tone"
              ><el-icon><component :is="item.icon" /></el-icon
            ></span>
            <span
              ><strong>{{ item.value }}</strong
              ><small>{{ item.label }}</small></span
            >
          </button>
        </div>

        <div class="workbench-grid">
          <div class="panel-card">
            <div class="card-heading">
              <div><strong>开始一次运维任务</strong><small>根据目标选择合适的执行能力</small></div>
            </div>
            <div class="type-grid">
              <button
                v-for="type in availableJobTypes"
                :key="type.key"
                type="button"
                class="type-card"
                @click="openCreate(type.key)"
              >
                <span class="type-icon" :class="type.key"
                  ><el-icon><component :is="type.icon" /></el-icon
                ></span>
                <span class="type-copy"
                  ><strong>{{ type.label }}</strong
                  ><small>{{ type.description }}</small></span
                >
                <el-tag
                  size="small"
                  :type="type.readOnly ? 'success' : type.key === 'power' ? 'danger' : 'warning'"
                  effect="plain"
                >
                  {{ type.readOnly ? '只读' : type.key === 'power' ? '严重风险' : '高风险' }}
                </el-tag>
              </button>
            </div>
          </div>

          <div class="panel-card">
            <div class="card-heading">
              <div><strong>最近任务</strong><small>统一展示所有自动化执行记录</small></div>
              <el-button link type="primary" @click="section = 'history'">查看全部</el-button>
            </div>
            <div v-loading="jobsLoading" class="recent-list">
              <button
                v-for="job in recentJobs"
                :key="job.id"
                type="button"
                class="recent-item"
                @click="openDetail(job.id)"
              >
                <span class="job-type-icon" :class="job.job_type"
                  ><el-icon><component :is="jobTypeMeta[job.job_type].icon" /></el-icon
                ></span>
                <span class="recent-main"
                  ><strong>{{ job.name }}</strong
                  ><small
                    >{{ jobTypeMeta[job.job_type].label }} · {{ job.target_total }} 台 ·
                    {{ fmtTime(job.created_at) }}</small
                  ></span
                >
                <el-tag size="small" :type="statusType(job.status)" effect="plain">{{
                  statusLabel(job.status)
                }}</el-tag>
              </button>
              <el-empty v-if="!jobsLoading && recentJobs.length === 0" description="暂无自动化任务" :image-size="56" />
            </div>
          </div>
        </div>
      </section>

      <section v-else-if="section === 'create'" class="automation-page create-page">
        <el-steps :active="createStep" align-center finish-status="success" class="create-steps">
          <el-step title="任务类型" /><el-step title="目标设备" /><el-step title="任务配置" /><el-step
            title="确认执行"
          />
        </el-steps>

        <div class="create-surface">
          <template v-if="createStep === 0">
            <div class="step-heading"><strong>选择任务类型</strong><span>不同类型具有独立的权限和安全边界</span></div>
            <div class="type-grid create-type-grid">
              <button
                v-for="type in availableJobTypes"
                :key="type.key"
                type="button"
                class="type-card"
                :class="{ selected: form.jobType === type.key }"
                @click="form.jobType = type.key"
              >
                <span class="type-icon" :class="type.key"
                  ><el-icon><component :is="type.icon" /></el-icon
                ></span>
                <span class="type-copy"
                  ><strong>{{ type.label }}</strong
                  ><small>{{ type.description }}</small></span
                >
                <el-icon v-if="form.jobType === type.key" class="selected-check"><CircleCheckFilled /></el-icon>
              </button>
            </div>
          </template>

          <template v-else-if="createStep === 1">
            <div class="step-heading">
              <strong>选择目标</strong
              ><span>{{
                form.jobType === 'agent' ? 'Agent 智能诊断仅支持单台设备' : '可选择多台设备/虚拟机批量执行'
              }}</span>
            </div>
            <div class="device-toolbar">
              <span class="selected-counter">已选择 {{ form.deviceIds.length }} 个目标</span>
              <el-button v-if="form.deviceIds.length" link type="danger" @click="form.deviceIds = []">清空</el-button>
            </div>
            <!-- 目标选择器内联展示:机房设备 + 虚拟机,勾选即生效 -->
            <DeviceSelector
              :selected-ids="form.deviceIds"
              :selectable-fn="selectorSelectable"
              @confirm="onSelectorConfirm"
            />
            <!-- 已选目标摘要 -->
            <div v-if="selectedDevices.length" class="selected-chips">
              <el-tag
                v-for="d in selectedDevices"
                :key="d.id"
                closable
                class="selected-chip"
                :type="d.target_type === 'pve_guest' ? 'warning' : 'info'"
                effect="plain"
                @close="removeTarget(d.id)"
                >{{ d.name }}</el-tag
              >
            </div>
          </template>

          <template v-else-if="createStep === 2">
            <div class="step-heading">
              <strong>配置{{ currentJobMeta.label }}</strong
              ><span>{{ currentJobMeta.description }}</span>
            </div>
            <el-form label-position="top" class="job-form">
              <el-form-item label="任务名称" required
                ><el-input v-model="form.name" maxlength="255" show-word-limit placeholder="填写便于识别的任务名称"
              /></el-form-item>

              <template v-if="form.jobType === 'agent'">
                <el-form-item label="需要排查的问题" required
                  ><el-input
                    v-model="form.question"
                    type="textarea"
                    :rows="5"
                    maxlength="500"
                    show-word-limit
                    placeholder="例如：服务器近期响应缓慢，请检查 CPU、内存、磁盘 IO 和异常服务"
                /></el-form-item>
                <div class="preset-row">
                  <button
                    v-for="question in agentPresets"
                    :key="question"
                    type="button"
                    @click="form.question = question"
                  >
                    {{ question }}
                  </button>
                </div>
              </template>

              <template v-else-if="form.jobType === 'inspection'">
                <!-- 检查项由后端按设备操作系统自动决定，这里不再让用户挑模式 / 勾选项 -->
                <el-form-item label="检查内容">
                  <span class="field-hint">自动检查 CPU、内存、磁盘等 6 项核心指标，按设备操作系统自动适配。</span>
                </el-form-item>
                <el-form-item label="单项超时"
                  ><el-input-number v-model="form.timeout" :controls="false" :min="5" :max="300" /><span
                    class="field-unit"
                    >秒</span
                  ></el-form-item
                >
              </template>

              <template v-else-if="form.jobType === 'script'">
                <el-form-item label="执行命令" required
                  ><el-input
                    v-model="form.command"
                    type="textarea"
                    :rows="6"
                    placeholder="输入需要在目标设备上执行的命令"
                    class="command-input"
                /></el-form-item>
                <div class="preset-row">
                  <button
                    v-for="preset in scriptPresets"
                    :key="preset.label"
                    type="button"
                    @click="form.command = preset.command"
                  >
                    {{ preset.label }}
                  </button>
                </div>
                <el-form-item label="执行超时"
                  ><el-input-number v-model="form.timeout" :controls="false" :min="5" :max="300" /><span
                    class="field-unit"
                    >秒</span
                  ></el-form-item
                >
              </template>

              <template v-else>
                <el-form-item label="电源操作" required>
                  <div class="power-options">
                    <button
                      type="button"
                      :class="{ selected: form.powerAction === 'reboot' }"
                      @click="form.powerAction = 'reboot'"
                    >
                      <el-icon><RefreshRight /></el-icon><strong>重启设备</strong
                      ><small>设备将短暂离线后重新启动</small>
                    </button>
                    <button
                      type="button"
                      :class="{ selected: form.powerAction === 'shutdown' }"
                      @click="form.powerAction = 'shutdown'"
                    >
                      <el-icon><SwitchButton /></el-icon><strong>关闭设备</strong
                      ><small>关闭后需要人工或带外方式启动</small>
                    </button>
                  </div>
                </el-form-item>
              </template>

              <el-divider />
              <el-form-item label="执行方式">
                <el-radio-group v-model="form.executionMode">
                  <el-radio-button value="now">立即执行</el-radio-button>
                  <el-radio-button value="once">定时执行</el-radio-button>
                  <el-radio-button value="recurring">周期执行</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item v-if="form.executionMode === 'once'" label="执行时间" required>
                <el-date-picker
                  v-model="form.scheduledAt"
                  type="datetime"
                  placeholder="选择未来的执行时间"
                  style="width: 100%"
                />
              </el-form-item>
              <el-form-item v-if="form.executionMode === 'recurring'" label="执行周期" required>
                <CronSchedulePicker v-model="form.cronExpression" />
              </el-form-item>
              <!-- 只有健康巡检会推 Webhook 通知(摘要 + PDF 报告);Agent/脚本/电源不发。 -->
              <el-form-item v-if="form.jobType === 'inspection'" label="Webhook 通知">
                <el-switch v-model="form.webhookNotify" />
              </el-form-item>
            </el-form>
          </template>

          <template v-else>
            <div class="step-heading"><strong>确认任务信息</strong><span>执行后可在任务详情中持续查看进度</span></div>
            <el-alert
              :type="currentJobMeta.readOnly ? 'success' : 'warning'"
              :closable="false"
              show-icon
              class="risk-alert"
              ><template #title>{{ riskTitle }}</template></el-alert
            >
            <div class="confirm-grid">
              <div>
                <span>任务名称</span><strong>{{ form.name }}</strong>
              </div>
              <div>
                <span>任务类型</span><strong>{{ currentJobMeta.label }}</strong>
              </div>
              <div>
                <span>目标设备</span><strong>{{ form.deviceIds.length }} 台</strong>
              </div>
              <div>
                <span>执行方式</span><strong>{{ executionModeLabel }}</strong>
              </div>
            </div>
            <div class="selected-devices-preview">
              <el-tag v-for="device in selectedDevices" :key="device.id" effect="plain">{{ device.name }}</el-tag>
            </div>
            <el-checkbox v-if="!currentJobMeta.readOnly" v-model="riskConfirmed" class="risk-confirm"
              >我已核对目标设备和执行内容，并了解该操作可能造成的影响</el-checkbox
            >
          </template>
        </div>

        <div class="create-footer">
          <el-button v-if="createStep > 0" @click="createStep--">上一步</el-button>
          <el-button v-if="createStep < 3" type="primary" :disabled="!canNext" @click="nextStep">下一步</el-button>
          <el-button
            v-else
            type="primary"
            :loading="creating"
            :disabled="!currentJobMeta.readOnly && !riskConfirmed"
            @click="createJob"
            >{{ form.executionMode === 'now' ? '确认并执行' : '创建计划' }}</el-button
          >
        </div>
      </section>

      <section v-else-if="section === 'history'" class="automation-page history-page">
        <div class="history-toolbar">
          <el-select v-model="historyType" clearable placeholder="任务类型" @change="loadJobs"
            ><el-option v-for="type in availableJobTypes" :key="type.key" :label="type.label" :value="type.key"
          /></el-select>
          <el-select v-model="historyStatus" clearable placeholder="执行状态" @change="loadJobs"
            ><el-option label="等待中" value="pending" /><el-option label="执行中" value="running" /><el-option
              label="已完成"
              value="completed" /><el-option label="部分异常" value="partial" /><el-option
              label="执行失败"
              value="failed"
          /></el-select>
          <el-button :loading="jobsLoading" @click="loadJobs"
            ><el-icon><Refresh /></el-icon>刷新</el-button
          >
        </div>
        <el-table v-loading="jobsLoading" :data="jobs" row-key="id" class="job-table" @row-click="onJobRowClick">
          <el-table-column label="任务" min-width="220"
            ><template #default="{ row }"
              ><div class="table-job">
                <span class="job-type-icon" :class="row.job_type"
                  ><el-icon><component :is="jobMetaFor(row.job_type).icon" /></el-icon></span
                ><span
                  ><strong>{{ row.name }}</strong
                  ><small>#{{ row.id }} · {{ jobMetaFor(row.job_type).label }}</small></span
                >
              </div></template
            ></el-table-column
          >
          <el-table-column label="目标" width="100" align="center"
            ><template #default="{ row }">{{ row.target_total }} 台</template></el-table-column
          >
          <el-table-column label="进度" min-width="150"
            ><template #default="{ row }"
              ><el-progress :percentage="jobProgress(row)" :status="progressStatus(row.status)" /></template
          ></el-table-column>
          <el-table-column label="状态" width="110" align="center"
            ><template #default="{ row }"
              ><el-tag :type="statusType(row.status)" effect="plain">{{ statusLabel(row.status) }}</el-tag></template
            ></el-table-column
          >
          <el-table-column prop="created_by_name" label="执行人" width="120" show-overflow-tooltip /><el-table-column
            label="创建时间"
            width="180"
            ><template #default="{ row }">{{ fmtDate(row.created_at) }}</template></el-table-column
          >
          <el-table-column label="操作" width="80" align="center"
            ><template #default="{ row }"
              ><el-button link type="primary" @click.stop="openDetail(row.id)">详情</el-button></template
            ></el-table-column
          >
        </el-table>
        <div v-if="jobsTotal > pageSize" class="pagination-row">
          <el-pagination
            v-model:current-page="page"
            :page-size="pageSize"
            :total="jobsTotal"
            layout="prev, pager, next"
            @current-change="loadJobs"
          />
        </div>
      </section>

      <section v-else class="automation-page schedules-page">
        <div class="history-toolbar">
          <el-button :loading="schedulesLoading" @click="loadSchedules"
            ><el-icon><Refresh /></el-icon>刷新</el-button
          >
        </div>
        <el-table v-loading="schedulesLoading" :data="schedules" row-key="id">
          <el-table-column prop="name" label="计划名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="任务类型" width="150"
            ><template #default="{ row }">{{ jobMetaFor(row.job_type).label }}</template></el-table-column
          >
          <el-table-column label="目标" width="90" align="center"
            ><template #default="{ row }">{{ row.target_ids.length }} 台</template></el-table-column
          >
          <el-table-column label="执行周期" min-width="180"
            ><template #default="{ row }">
              <span v-if="row.schedule_type === 'once'">{{ fmtDate(row.scheduled_at) }}</span>
              <!-- 列表里显示中文周期，原始表达式放到 title 里备查 -->
              <span v-else :title="row.cron_expression || ''">{{ humanizeCron(row.cron_expression) }}</span>
            </template></el-table-column
          >
          <el-table-column label="下次执行" width="180"
            ><template #default="{ row }">{{
              row.next_run_at ? fmtDate(row.next_run_at) : '-'
            }}</template></el-table-column
          >
          <el-table-column label="状态" width="100" align="center"
            ><template #default="{ row }"
              ><el-tag :type="scheduleStatusTag(row.status)" effect="plain">{{
                scheduleStatusLabel(row.status)
              }}</el-tag></template
            ></el-table-column
          >
          <el-table-column label="操作" width="170" align="center"
            ><template #default="{ row }"
              ><el-button link type="primary" @click="openScheduleEdit(row)">编辑</el-button
              ><el-button v-if="row.status === 'active'" link type="warning" @click="pauseSchedule(row)">暂停</el-button
              ><el-button v-if="row.status === 'paused'" link type="success" @click="resumeSchedule(row)"
                >启用</el-button
              ><el-button link type="danger" @click="removeSchedule(row)">删除</el-button></template
            ></el-table-column
          >
        </el-table>
        <el-empty
          v-if="!schedulesLoading && schedules.length === 0"
          description="暂无定时计划，可在新建任务时选择定时或周期执行"
          :image-size="64"
        />
      </section>
    </div>

    <!-- 计划编辑弹窗:名称/目标/配置/周期。与创建向导同组件(DeviceSelector/CronSchedulePicker),
         预填现值,提交走 PUT;job_type 不可改(后端口径一致)。 -->
    <el-dialog
      v-model="scheduleEditVisible"
      :title="`编辑计划 · ${editingSchedule?.name || ''}`"
      width="720px"
      :close-on-click-modal="false"
    >
      <div v-loading="devicesLoading" class="schedule-edit-body">
        <el-form label-position="top" class="job-form">
          <el-form-item label="计划名称" required
            ><el-input v-model="scheduleEditForm.name" maxlength="255" show-word-limit
          /></el-form-item>
          <el-form-item label="目标"
            ><span class="field-hint">已选 {{ scheduleEditForm.deviceIds.length }} 个目标</span>
            <DeviceSelector
              :selected-ids="scheduleEditForm.deviceIds"
              :selectable-fn="selectorSelectable"
              @confirm="onEditSelectorConfirm"
          /></el-form-item>
          <template v-if="editingSchedule?.job_type === 'agent'">
            <el-form-item label="诊断问题" required
              ><el-input v-model="scheduleEditForm.question" type="textarea" :rows="4" maxlength="500" show-word-limit
            /></el-form-item>
          </template>
          <template v-else-if="editingSchedule?.job_type === 'inspection'">
            <el-form-item label="单项超时(秒)"
              ><el-input-number v-model="scheduleEditForm.timeout" :controls="false" :min="5" :max="300"
            /></el-form-item>
          </template>
          <template v-else-if="editingSchedule?.job_type === 'script'">
            <el-form-item label="执行命令" required
              ><el-input
                v-model="scheduleEditForm.command"
                type="textarea"
                :rows="4"
                placeholder="输入需要在目标设备上执行的命令"
                class="command-input"
            /></el-form-item>
            <el-form-item label="执行超时(秒)"
              ><el-input-number v-model="scheduleEditForm.timeout" :controls="false" :min="5" :max="300"
            /></el-form-item>
          </template>
          <template v-else>
            <el-form-item label="电源操作" required>
              <el-radio-group v-model="scheduleEditForm.powerAction">
                <el-radio-button value="reboot">重启</el-radio-button>
                <el-radio-button value="shutdown">关机</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </template>
          <el-form-item label="执行周期" required
            ><CronSchedulePicker v-model="scheduleEditForm.cronExpression"
          /></el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="scheduleEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="scheduleSaving" :disabled="!canSaveScheduleEdit" @click="saveScheduleEdit"
          >保存</el-button
        >
      </template>
    </el-dialog>

    <el-drawer
      v-model="detailVisible"
      size="720px"
      direction="rtl"
      :title="detailJob?.name || '任务详情'"
      @closed="stopDetailPoll"
    >
      <div v-loading="detailLoading" class="job-detail">
        <template v-if="detailJob">
          <div class="detail-summary">
            <el-tag :type="statusType(detailJob.status)" size="large" effect="plain">{{
              statusLabel(detailJob.status)
            }}</el-tag
            ><span>{{ jobTypeMeta[detailJob.job_type].label }}</span
            ><span>{{ detailJob.targets.length }} 台设备</span><span>{{ fmtDate(detailJob.created_at) }}</span>
          </div>
          <div v-if="detailJob.job_type === 'agent' && detailJob.config_json?.question" class="detail-question">
            <span class="detail-question-label">诊断问题</span>
            <p>{{ detailJob.config_json.question }}</p>
          </div>
          <el-progress :percentage="detailProgress" :status="progressStatus(detailJob.status)" :stroke-width="10" />
          <div class="target-list">
            <div v-for="target in detailJob.targets" :key="target.id" class="target-card">
              <div class="target-head">
                <span class="status-dot" :class="`status-${target.status}`" /><span
                  ><strong>{{ target.device_name }}</strong
                  ><small>{{ target.device_ip || '-' }}</small></span
                ><el-tag size="small" :type="targetStatusType(target.status)" effect="plain">{{
                  targetStatusLabel(target.status)
                }}</el-tag
                ><span class="target-duration">{{
                  target.duration_ms != null ? fmtDuration(target.duration_ms) : ''
                }}</span>
              </div>
              <!-- 巡检结果统一交给 InspectionResultCard（状态点 + 中文标签 + 数值 + 原始输出弹窗），
                   不再把 step 的英文键和命令原文直接糊在抽屉里 -->
              <template v-if="detailJob.job_type === 'inspection'">
                <InspectionResultCard v-if="inspectionResults[target.id]" :result="inspectionResults[target.id]" />
                <!-- 巡检记录还没拉到（或拉取失败）时用 result_json 里现成的计数兜底；
                     卡片就绪后只剩卡片头部的计数，两处不会同时出现 -->
                <div v-else-if="toSummaryCells(target.result_json).length" class="inspection-summary">
                  <span v-for="cell in toSummaryCells(target.result_json)" :key="cell.text" :class="cell.tone">
                    {{ cell.text }}
                  </span>
                </div>
              </template>
              <!-- 执行明细：step.output 是命令原文，只适合排障。
                   巡检任务在结果卡片就绪时隐藏它——卡片每项已有「原始输出」弹窗，
                   两个入口内容重叠、反而困惑；只在卡片拉取失败时保留作兑底。
                   非巡检任务（agent/script/power）没有结果卡片，始终保留。 -->
              <el-collapse
                v-if="target.steps.length && (detailJob.job_type !== 'inspection' || !inspectionResults[target.id])"
                v-model="expandedStepPanels"
                class="step-collapse"
              >
                <el-collapse-item :name="`target-${target.id}`" title="查看执行明细">
                  <div class="step-list">
                    <div v-for="step in target.steps" :key="step.id" class="step-row">
                      <el-icon :class="step.status === 'failed' ? 'step-failed' : 'step-ok'"
                        ><CircleCheck v-if="step.status !== 'failed'" /><CircleClose v-else /></el-icon
                      ><span class="step-name">{{ step.step_name }}</span
                      ><code v-if="step.exit_code != null">exit {{ step.exit_code }}</code>
                      <el-collapse v-if="step.output || step.error_message" class="step-output"
                        ><el-collapse-item title="查看输出">
                          <pre :class="{ error: step.error_message }">{{ step.error_message || step.output }}</pre>
                        </el-collapse-item></el-collapse
                      >
                    </div>
                  </div>
                </el-collapse-item>
              </el-collapse>
              <div
                v-if="target.result_json?.report"
                class="agent-report"
                v-html="renderMarkdown(String(target.result_json.report))"
              />
              <el-alert
                v-if="target.error_message && !target.steps.length"
                type="error"
                :title="target.error_message"
                :closable="false"
                show-icon
              />
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch, type Component } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatLineSquare,
  CircleCheck,
  CircleCheckFilled,
  CircleClose,
  Clock,
  DataAnalysis,
  Loading,
  MagicStick,
  Plus,
  Promotion,
  Refresh,
  RefreshRight,
  SwitchButton,
  Warning,
} from '@element-plus/icons-vue'
import { automationAPI, inspectionAPI } from '@/api'
import InspectionResultCard from './InspectionResultCard.vue'
import CronSchedulePicker from './CronSchedulePicker.vue'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import { useAuthStore } from '@/stores/auth'
import { jobProgress, targetsProgress } from '@/utils/automationProgress'
import { toDeviceResult, toSummaryCells } from '@/utils/inspectionAdapter'
import { humanizeCron, isCronShape } from '@/utils/cron'
import { renderMarkdown } from '@/utils/sanitize'
import type {
  AutomationDevice,
  AutomationJob,
  AutomationJobListItem,
  AutomationJobStatus,
  AutomationJobType,
  AutomationSchedule,
  AutomationTarget,
} from '@/types/automation'
import type { InspectionDeviceResult } from '@/types/inspection'

const props = withDefaults(defineProps<{ tab?: string }>(), { tab: '' })
const authStore = useAuthStore()
const sections = [
  { key: 'overview', label: '运维概览', icon: MagicStick },
  { key: 'create', label: '新建任务', icon: Plus },
  { key: 'history', label: '执行记录', icon: Clock },
  { key: 'schedules', label: '定时计划', icon: Clock },
] as const
const section = ref<'overview' | 'create' | 'history' | 'schedules'>('overview')

interface JobTypeMeta {
  key: AutomationJobType
  label: string
  description: string
  icon: Component
  readOnly: boolean
  permission: string
}
const allJobTypes: JobTypeMeta[] = [
  {
    key: 'agent',
    label: 'Agent 智能诊断',
    description: '用自然语言描述问题，由 Agent 按需执行只读诊断并生成报告',
    icon: ChatLineSquare,
    readOnly: true,
    permission: 'device:remote',
  },
  {
    key: 'inspection',
    label: '健康巡检',
    description: '按标准检查项批量验证设备健康状态并识别异常',
    icon: DataAnalysis,
    readOnly: true,
    permission: 'automation:manage',
  },
  {
    key: 'script',
    label: '批量执行',
    description: '在多台设备上执行明确的 Shell 或 PowerShell 命令',
    icon: Promotion,
    readOnly: false,
    permission: 'automation:manage',
  },
  {
    key: 'power',
    label: '电源操作',
    description: '批量重启或关闭目标服务器，并验证设备离线状态',
    icon: SwitchButton,
    readOnly: false,
    permission: 'automation:manage',
  },
]
const availableJobTypes = computed(() => allJobTypes.filter((item) => authStore.hasPermission(item.permission)))
const jobTypeMeta = Object.fromEntries(allJobTypes.map((item) => [item.key, item])) as Record<
  AutomationJobType,
  JobTypeMeta
>

const jobs = ref<AutomationJobListItem[]>([])
const jobsTotal = ref(0)
const jobsLoading = ref(false)
const historyType = ref<AutomationJobType | ''>('')
const historyStatus = ref('')
const page = ref(1)
const pageSize = 20
const recentJobs = computed(() => jobs.value.slice(0, 6))
const schedules = ref<AutomationSchedule[]>([])
const schedulesLoading = ref(false)
const overview = computed(() => ({
  running: jobs.value.filter((item) => ['pending', 'running'].includes(item.status)).length,
  completed: jobs.value.filter((item) => item.status === 'completed').length,
  partial: jobs.value.filter((item) => item.status === 'partial').length,
  failed: jobs.value.filter((item) => item.status === 'failed').length,
}))
const summaryCards = computed(() => [
  { status: 'running', label: '执行中任务', value: overview.value.running, icon: Loading, tone: 'running' },
  { status: 'completed', label: '已完成任务', value: overview.value.completed, icon: CircleCheck, tone: 'success' },
  { status: 'partial', label: '部分异常', value: overview.value.partial, icon: Warning, tone: 'warning' },
  { status: 'failed', label: '执行失败', value: overview.value.failed, icon: CircleClose, tone: 'danger' },
])

const devices = ref<AutomationDevice[]>([])
const devicesLoading = ref(false)
// 设备选择改为分组选择器(机房/虚拟化),旧的搜索/状态过滤不再需要。
// devices 仍需保留用于按 id 反查名称/凭据(已选摘要、selectable 判断)。
const selectedDevices = computed(() => devices.value.filter((device) => form.deviceIds.includes(device.id)))

const createStep = ref(0)
const creating = ref(false)
const riskConfirmed = ref(false)
const form = reactive({
  jobType: 'agent' as AutomationJobType,
  name: '',
  deviceIds: [] as number[],
  question: '',
  command: '',
  timeout: 30,
  powerAction: 'reboot' as 'reboot' | 'shutdown',
  executionMode: 'now' as 'now' | 'once' | 'recurring',
  scheduledAt: '' as string | Date,
  cronExpression: '',
  // 任务结束后是否推送 Webhook 通知（存入 config_json.webhook_notify）。
  // 仅健康巡检有效；兼容旧键 report_webhook（后端两者都读）。默认开：
  // 用户心智是「配置过就该一直发」，每次建任务重置为关会导致「以为开了却没发」。
  webhookNotify: true,
})
const currentJobMeta = computed(() => jobTypeMeta[form.jobType])
const agentPresets = [
  '全面检查系统健康状态并给出建议',
  '排查 CPU 和内存占用异常',
  '检查磁盘空间、磁盘 IO 与异常日志',
  '排查服务不可用或响应缓慢的原因',
]
const scriptPresets = [
  { label: '查看主机名', command: 'hostname' },
  { label: '查看磁盘使用', command: 'df -h' },
  { label: '查看内存使用', command: 'free -m' },
  { label: '查看系统负载', command: 'uptime' },
]
const riskTitle = computed(() =>
  currentJobMeta.value.readOnly
    ? '只读任务，不会修改设备状态'
    : form.jobType === 'power'
      ? '严重风险操作，请核对影响范围'
      : '自定义脚本可能修改设备状态',
)
const executionModeLabel = computed(
  () => (({ now: '立即执行', once: '定时执行', recurring: '周期执行' }) as Record<string, string>)[form.executionMode],
)
const canNext = computed(() => {
  if (createStep.value === 0) return !!form.jobType
  if (createStep.value === 1) return form.deviceIds.length > 0
  if (createStep.value === 2) {
    if (!form.name.trim()) return false
    if (form.jobType === 'agent' && form.question.trim().length < 2) return false
    if (form.jobType === 'script' && !form.command.trim()) return false
    if (form.executionMode === 'once') return !!form.scheduledAt
    // 高级模式下可能手写了非法 cron，在这里拦住，不要等后端报错
    if (form.executionMode === 'recurring') return isCronShape(form.cronExpression)
    return true
  }
  return true
})

function resetForm(jobType?: AutomationJobType) {
  form.jobType = jobType || availableJobTypes.value[0]?.key || 'agent'
  form.name = ''
  form.deviceIds = []
  form.question = ''
  form.command = ''
  form.timeout = 30
  form.powerAction = 'reboot'
  form.executionMode = 'now'
  form.scheduledAt = ''
  form.cronExpression = ''
  form.webhookNotify = true
  createStep.value = jobType ? 1 : 0
  riskConfirmed.value = false
}
function openCreate(jobType?: AutomationJobType) {
  resetForm(jobType)
  section.value = 'create'
  if (!devices.value.length) void loadDevices()
}
function openHistory(status = '') {
  historyStatus.value = status
  section.value = 'history'
  page.value = 1
  void loadJobs()
}
// 选择器内的可选判断:按 id 反查 devices(选择器只传 id)。
// PVE 虚拟机:电源操作始终可选(走 PVE API,无需 guest 凭据);其余任务类型
// (巡检/批量脚本/Agent)都是连进 guest 内部执行(SSH/WinRM),必须先配置好
// 运维接入凭据和 IP——与后端 create_job 对 pve_guest 非 power 任务的校验一致。
// 普通设备:与 PVE 虚拟机口径统一——没绑凭据/没 IP 的一律不可选。
// Windows 设备额外看通道连通信号:指标采集对 Windows 走的就是 WinRM,
// 「最近采集明确失败」= WinRM 确认不通 → 不可选(修好或下一轮采集成功
// 自动恢复);「还没采集过/成功」不拦,避免误伤刚恢复的机器。
function selectorSelectable(id: number, isGuest: boolean): boolean {
  const device = devices.value.find((d) => d.id === id)
  if (!device) return false
  if (device.target_type === 'pve_guest' || isGuest) {
    if (form.jobType === 'power') return true
    return !!device.has_credential && !!device.ip_address
  }
  return !!device.has_credential && !!device.ip_address && !device.metrics_failed
}

function onSelectorConfirm(ids: number[]) {
  // Agent 智能诊断只允许单台:取第一个。
  form.deviceIds = form.jobType === 'agent' ? ids.slice(0, 1) : ids
}

function removeTarget(id: number) {
  form.deviceIds = form.deviceIds.filter((x) => x !== id)
}

function nextStep() {
  if (!canNext.value) return
  createStep.value++
}
async function loadDevices() {
  devicesLoading.value = true
  try {
    const res = await automationAPI.devices()
    devices.value = res.data
  } catch {
    ElMessage.error('加载设备列表失败')
  } finally {
    devicesLoading.value = false
  }
}

async function createJob() {
  creating.value = true
  try {
    const config: Record<string, any> =
      form.jobType === 'agent'
        ? { question: form.question }
        : form.jobType === 'inspection'
          ? // 巡检项由后端 core 模式按设备操作系统自动决定，前端不再传 items
            { mode: 'core', timeout: form.timeout }
          : form.jobType === 'script'
            ? { command: form.command, timeout: form.timeout }
            : { action: form.powerAction }
    // Webhook 通知仅健康巡检支持；其它任务类型不带该键，避免误导
    if (form.jobType === 'inspection') config.webhook_notify = form.webhookNotify
    if (form.executionMode === 'now') {
      const res = await automationAPI.createJob({
        name: form.name,
        job_type: form.jobType,
        device_ids: form.deviceIds,
        config,
      })
      ElMessage.success('任务已创建，正在后台执行')
      await loadJobs()
      section.value = 'history'
      await openDetail(res.data.id)
    } else {
      const scheduledAt =
        form.scheduledAt instanceof Date ? form.scheduledAt.toISOString() : form.scheduledAt || undefined
      await automationAPI.createSchedule({
        name: form.name,
        job_type: form.jobType,
        device_ids: form.deviceIds,
        config,
        schedule_type: form.executionMode,
        scheduled_at: scheduledAt,
        cron_expression: form.cronExpression || undefined,
      })
      ElMessage.success('定时计划已创建')
      await loadSchedules()
      section.value = 'schedules'
    }
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '创建任务失败')
  } finally {
    creating.value = false
  }
}
async function loadJobs() {
  jobsLoading.value = true
  try {
    const res = await automationAPI.jobs({
      job_type: historyType.value || undefined,
      status: historyStatus.value || undefined,
      page: page.value,
      page_size: pageSize,
    })
    jobs.value = res.data.items
    jobsTotal.value = res.data.total
  } catch {
    ElMessage.error('加载任务记录失败')
  } finally {
    jobsLoading.value = false
  }
  scheduleJobsPoll()
}

// 历史页轻量轮询:存在 pending/running 任务时每 5s 静默刷新列表
// (进度条/状态随 steps_executed 实时爬升);没有在跑的任务就停表。
// 静默刷新不置 jobsLoading,避免进度条刷新时表格整体转圈。
let jobsTimer: number | undefined
async function refreshJobsQuietly() {
  try {
    // 概览页的统计卡/最近任务基于无筛选列表;历史页带用户设置的筛选。
    // 在历史页设了筛选再切回概览时,若继续带筛选刷新,概览数字会错。
    const inHistory = section.value === 'history'
    const res = await automationAPI.jobs({
      job_type: inHistory ? historyType.value || undefined : undefined,
      status: inHistory ? historyStatus.value || undefined : undefined,
      page: inHistory ? page.value : 1,
      page_size: pageSize,
    })
    jobs.value = res.data.items
    jobsTotal.value = res.data.total
  } catch {
    return // 静默轮询失败不打扰用户;下一轮再试
  }
  scheduleJobsPoll()
}
function scheduleJobsPoll() {
  if (jobsTimer) window.clearTimeout(jobsTimer)
  jobsTimer = undefined
  const hasActive = jobs.value.some((j) => ['pending', 'running'].includes(j.status))
  // 概览页的统计卡同样需要跟随任务推进(曾只认 history,概览数字冻结到手动刷新)
  if (!hasActive || (section.value !== 'history' && section.value !== 'overview')) return
  jobsTimer = window.setTimeout(() => {
    if (!document.hidden) void refreshJobsQuietly()
    else scheduleJobsPoll()
  }, 5000)
}
function stopJobsPoll() {
  if (jobsTimer) window.clearTimeout(jobsTimer)
  jobsTimer = undefined
}

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailJob = ref<AutomationJob | null>(null)
let detailTimer: number | undefined
let detailRequestInFlight = false
// 与列表页的 jobProgress 共用同一份终态口径（completed / warning / failed）
const detailProgress = computed(() => (detailJob.value ? targetsProgress(detailJob.value.targets) : 0))
// target.id → 适配后的巡检结果，交给 InspectionResultCard 渲染
const inspectionResults = ref<Record<number, InspectionDeviceResult>>({})
// 已发起过详情请求的巡检记录 id：轮询每 1.5s 重跑一次 loadDetail，靠它避免重复拉取
const requestedRecordIds = new Set<number>()
// 「执行明细」折叠面板的展开项
const expandedStepPanels = ref<string[]>([])

function recordIdOf(target: AutomationTarget) {
  return Number(target.result_json?.record_id ?? 0)
}
/**
 * 巡检任务：按 target.result_json.record_id 拉取完整巡检记录，转成卡片需要的形状。
 * 多台设备并发拉取；单台失败只影响它自己（回退到汇总条 + 执行明细），不会让整个抽屉白屏。
 */
async function loadInspectionResults(job: AutomationJob) {
  if (job.job_type !== 'inspection') return
  const pending = job.targets.filter((target) => {
    const recordId = recordIdOf(target)
    return recordId > 0 && !requestedRecordIds.has(recordId)
  })
  if (!pending.length) return
  for (const target of pending) requestedRecordIds.add(recordIdOf(target))
  const settled = await Promise.allSettled(
    pending.map(async (target) => ({
      targetId: target.id,
      record: (await inspectionAPI.getRecordDetail(recordIdOf(target))).data,
    })),
  )
  const fetched: Record<number, InspectionDeviceResult> = {}
  for (const item of settled) {
    if (item.status === 'fulfilled') fetched[item.value.targetId] = toDeviceResult(item.value.record)
  }
  // 合并而不是覆盖：轮询期间已经拉到的结果不能被冲掉
  if (Object.keys(fetched).length) inspectionResults.value = { ...inspectionResults.value, ...fetched }
}
/**
 * 执行明细的默认展开状态：巡检任务的 step.output 是命令原文，可读结果已由卡片呈现，
 * 默认收起；脚本 / 电源 / Agent 任务的 step 本身就是主要结果，默认展开。
 * 只在打开抽屉时设置一次，轮询刷新不会覆盖用户手动改过的展开状态。
 */
function syncStepPanels() {
  const job = detailJob.value
  expandedStepPanels.value =
    job && job.job_type !== 'inspection' ? job.targets.map((target) => `target-${target.id}`) : []
}
async function openDetail(id: number) {
  detailVisible.value = true
  detailLoading.value = true
  stopDetailPoll()
  // 切换任务时清掉上一个任务的巡检结果缓存，避免抽屉里短暂串台
  inspectionResults.value = {}
  requestedRecordIds.clear()
  expandedStepPanels.value = []
  await loadDetail(id)
  detailLoading.value = false
  syncStepPanels()
}
async function loadDetail(id: number) {
  if (detailRequestInFlight) return
  detailRequestInFlight = true
  try {
    const res = await automationAPI.job(id)
    detailJob.value = res.data
    void loadInspectionResults(res.data)
    if (['pending', 'running'].includes(res.data.status))
      detailTimer = window.setTimeout(() => {
        if (!document.hidden) void loadDetail(id)
        else detailTimer = window.setTimeout(() => void loadDetail(id), 3000)
      }, 1500)
    else void loadJobs()
  } catch {
    ElMessage.error('加载任务详情失败')
    stopDetailPoll()
  } finally {
    detailRequestInFlight = false
  }
}
function stopDetailPoll() {
  if (detailTimer) window.clearTimeout(detailTimer)
  detailTimer = undefined
}

function statusLabel(status: string) {
  return (
    (
      {
        pending: '等待中',
        running: '执行中',
        completed: '已完成',
        partial: '部分异常',
        failed: '执行失败',
        cancelled: '已取消',
      } as Record<string, string>
    )[status] || status
  )
}
function jobMetaFor(type: AutomationJobType) {
  return jobTypeMeta[type]
}
function onJobRowClick(row: AutomationJobListItem) {
  void openDetail(row.id)
}
function statusType(status: string) {
  return (
    (
      {
        pending: 'info',
        running: 'primary',
        completed: 'success',
        partial: 'warning',
        failed: 'danger',
        cancelled: 'info',
      } as Record<string, string>
    )[status] || 'info'
  )
}
function targetStatusLabel(status: string) {
  return (
    (
      { pending: '等待中', running: '执行中', completed: '成功', warning: '存在异常', failed: '失败' } as Record<
        string,
        string
      >
    )[status] || status
  )
}
function targetStatusType(status: string) {
  return (
    (
      { pending: 'info', running: 'primary', completed: 'success', warning: 'warning', failed: 'danger' } as Record<
        string,
        string
      >
    )[status] || 'info'
  )
}
function progressStatus(status: AutomationJobStatus): 'success' | 'exception' | undefined {
  return status === 'completed' ? 'success' : status === 'failed' ? 'exception' : undefined
}
function fmtTime(value: string) {
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
function fmtDate(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
function fmtDuration(ms: number) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
async function loadSchedules() {
  schedulesLoading.value = true
  try {
    const res = await automationAPI.schedules()
    schedules.value = res.data
  } catch {
    ElMessage.error('加载定时计划失败')
  } finally {
    schedulesLoading.value = false
  }
}
async function removeSchedule(schedule: AutomationSchedule) {
  try {
    await ElMessageBox.confirm(`确定删除计划「${schedule.name}」吗？`, '删除计划', { type: 'warning' })
    await automationAPI.deleteSchedule(schedule.id)
    ElMessage.success('计划已删除')
    await loadSchedules()
  } catch {
    /* cancelled */
  }
}

// ── 计划编辑/暂停/恢复(旧 ScheduledTask 系统的能力迁入统一计划) ──
const scheduleEditVisible = ref(false)
const scheduleSaving = ref(false)
const editingSchedule = ref<AutomationSchedule | null>(null)
const scheduleEditForm = reactive({
  name: '',
  deviceIds: [] as number[],
  question: '',
  command: '',
  timeout: 30,
  powerAction: 'reboot' as 'reboot' | 'shutdown',
  cronExpression: '',
})

function scheduleStatusLabel(status: string) {
  return (
    (
      {
        active: '启用',
        paused: '已暂停',
        completed: '已完成',
        disabled: '已停用',
      } as Record<string, string>
    )[status] || status
  )
}
function scheduleStatusTag(status: string) {
  // disabled(授权失效系统停用)与 paused(用户主动暂停)分色:前者需要排查,后者随时可恢复
  if (status === 'active') return 'success'
  if (status === 'paused') return 'warning'
  return 'info'
}

async function openScheduleEdit(schedule: AutomationSchedule) {
  editingSchedule.value = schedule
  scheduleEditForm.name = schedule.name
  scheduleEditForm.deviceIds = [...(schedule.target_ids || [])]
  scheduleEditForm.question = String(schedule.config_json?.question || '')
  scheduleEditForm.command = String(schedule.config_json?.command || '')
  scheduleEditForm.timeout = Number(schedule.config_json?.timeout || 30)
  scheduleEditForm.powerAction = (schedule.config_json?.action as 'reboot' | 'shutdown') || 'reboot'
  scheduleEditForm.cronExpression = schedule.cron_expression || ''
  if (!devices.value.length) void loadDevices() // 编辑也要选目标,懒加载设备列表
  scheduleEditVisible.value = true
}

function onEditSelectorConfirm(ids: number[]) {
  scheduleEditForm.deviceIds = editingSchedule.value?.job_type === 'agent' ? ids.slice(0, 1) : ids
}

const canSaveScheduleEdit = computed(() => {
  if (!scheduleEditForm.name.trim()) return false
  if (!scheduleEditForm.deviceIds.length) return false
  if (!isCronShape(scheduleEditForm.cronExpression)) return false
  if (editingSchedule.value?.job_type === 'agent' && scheduleEditForm.question.trim().length < 2) return false
  if (editingSchedule.value?.job_type === 'script' && !scheduleEditForm.command.trim()) return false
  return true
})

async function saveScheduleEdit() {
  if (!editingSchedule.value || !canSaveScheduleEdit.value) return
  scheduleSaving.value = true
  try {
    const jt = editingSchedule.value.job_type
    const config: Record<string, any> =
      jt === 'agent'
        ? { question: scheduleEditForm.question }
        : jt === 'inspection'
          ? { mode: 'core', timeout: scheduleEditForm.timeout }
          : jt === 'script'
            ? { command: scheduleEditForm.command, timeout: scheduleEditForm.timeout }
            : { action: scheduleEditForm.powerAction }
    await automationAPI.updateSchedule(editingSchedule.value.id, {
      name: scheduleEditForm.name.trim(),
      device_ids: scheduleEditForm.deviceIds,
      config,
      schedule_type: 'recurring',
      cron_expression: scheduleEditForm.cronExpression,
    })
    ElMessage.success('计划已更新,下次执行时间已按新周期重算')
    scheduleEditVisible.value = false
    await loadSchedules()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '保存计划失败')
  } finally {
    scheduleSaving.value = false
  }
}

async function pauseSchedule(schedule: AutomationSchedule) {
  try {
    await automationAPI.setScheduleStatus(schedule.id, 'paused')
    ElMessage.success('计划已暂停,到期不再触发')
    await loadSchedules()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '暂停失败')
  }
}

async function resumeSchedule(schedule: AutomationSchedule) {
  try {
    await automationAPI.setScheduleStatus(schedule.id, 'active')
    ElMessage.success('计划已恢复,按周期继续执行')
    await loadSchedules()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '恢复失败')
  }
}

watch(
  () => props.tab,
  (tab) => {
    const map: Record<string, AutomationJobType> = { agent: 'agent', scripts: 'script', inspection: 'inspection' }
    if (map[tab]) openCreate(map[tab])
    else if (!tab || tab === 'overview') section.value = 'overview'
  },
  { immediate: true },
)
// 离开历史/概览页停掉任务列表轮询;回到这两页时若有在跑任务则重启
watch(section, (s) => {
  if (s !== 'history' && s !== 'overview') stopJobsPoll()
  else scheduleJobsPoll()
})
watch(
  () => form.jobType,
  () => {
    form.deviceIds = []
    riskConfirmed.value = false
  },
)
onMounted(() => {
  void Promise.all([loadJobs(), loadDevices(), loadSchedules()])
})
onUnmounted(() => {
  stopDetailPoll()
  stopJobsPoll()
})
</script>

<style scoped>
.automation-center {
  background: var(--dcn-bg-page);
}
.automation-header {
  justify-content: space-between;
}
.automation-header > div {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.automation-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0 var(--dcn-space-6) var(--dcn-space-6);
}
.automation-tabs {
  position: sticky;
  top: 0;
  z-index: 3;
  display: flex;
  gap: 6px;
  padding: 12px 0;
  background: var(--dcn-bg-page);
  border-bottom: 1px solid var(--dcn-border-light);
}
.automation-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 40px;
  padding: 0 16px;
  border: 0;
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-secondary);
  background: transparent;
  cursor: pointer;
  font: inherit;
}
.automation-tabs button:hover {
  color: var(--dcn-text-primary);
  background: var(--dcn-bg-hover);
}
.automation-tabs button.active {
  color: var(--dcn-primary);
  background: var(--dcn-primary-soft);
  font-weight: 600;
}
.automation-page {
  padding-top: var(--dcn-space-5);
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.summary-card {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 94px;
  padding: 18px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
  box-shadow: var(--dcn-shadow-sm);
}
.summary-card:hover {
  border-color: var(--dcn-border);
  transform: translateY(-1px);
}
.summary-card > span:last-child {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.summary-card strong {
  font-size: 26px;
  line-height: 1;
}
.summary-card small,
.card-heading small,
.type-copy small,
.recent-main small,
.device-copy small,
.table-job small,
.target-head small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.summary-icon,
.type-icon,
.job-type-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: var(--dcn-radius-md);
}
.summary-icon {
  width: 46px;
  height: 46px;
  font-size: 22px;
}
.summary-icon.running,
.type-icon.agent,
.job-type-icon.agent {
  color: var(--dcn-primary);
  background: var(--dcn-primary-soft);
}
.summary-icon.success,
.type-icon.inspection,
.job-type-icon.inspection {
  color: var(--dcn-success);
  background: color-mix(in srgb, var(--dcn-success) 12%, transparent);
}
.summary-icon.warning,
.type-icon.script,
.job-type-icon.script {
  color: var(--dcn-warning);
  background: color-mix(in srgb, var(--dcn-warning) 12%, transparent);
}
.summary-icon.danger,
.type-icon.power,
.job-type-icon.power {
  color: var(--dcn-danger);
  background: color-mix(in srgb, var(--dcn-danger) 12%, transparent);
}
.workbench-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr);
  gap: 16px;
  margin-top: 16px;
}
.panel-card,
.create-surface {
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
  box-shadow: var(--dcn-shadow-sm);
}
.panel-card {
  padding: 18px;
}
.card-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.card-heading > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.type-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.type-card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 82px;
  padding: 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
}
.type-card:hover,
.type-card.selected {
  border-color: var(--dcn-primary);
  background: var(--dcn-primary-soft);
}
.type-icon {
  width: 42px;
  height: 42px;
  font-size: 20px;
}
.type-copy {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}
.type-copy small {
  line-height: 1.45;
}
.selected-check {
  color: var(--dcn-primary);
  font-size: 19px;
}
.recent-list {
  min-height: 200px;
}
.recent-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 2px;
  border: 0;
  border-bottom: 1px solid var(--dcn-border-light);
  background: transparent;
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
}
.recent-item:last-child {
  border-bottom: 0;
}
.recent-item:hover .recent-main strong {
  color: var(--dcn-primary);
}
.job-type-icon {
  width: 34px;
  height: 34px;
}
.recent-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.recent-main strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.create-steps {
  max-width: 900px;
  margin: 0 auto 22px;
}
.create-surface {
  max-width: 1000px;
  min-height: 440px;
  margin: auto;
  padding: 24px;
}
.step-heading {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 20px;
}
.step-heading strong {
  font-size: 18px;
}
.step-heading span {
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.create-type-grid {
  max-width: 800px;
}
.device-toolbar {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) 160px auto;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}
.selected-counter {
  color: var(--dcn-primary);
  font-size: 13px;
  font-weight: 600;
}
.selected-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}
.selected-chip {
  max-width: 260px;
}
.selected-chip :deep(.el-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.device-list {
  max-height: 430px;
  overflow: auto;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
}
.device-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 60px;
  padding: 10px 14px;
  border: 0;
  border-bottom: 1px solid var(--dcn-border-light);
  background: var(--dcn-bg-card);
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
}
.device-row:last-child {
  border-bottom: 0;
}
.device-row:hover,
.device-row.selected {
  background: var(--dcn-primary-soft);
}
.device-row.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.device-check {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border: 1px solid var(--dcn-border);
  border-radius: 5px;
  color: var(--dcn-primary);
}
.device-row.selected .device-check {
  border-color: var(--dcn-primary);
}
.device-copy {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.status-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--dcn-text-disabled);
}
.status-online,
.status-completed {
  background: var(--dcn-success);
}
.status-running {
  background: var(--dcn-primary);
  box-shadow: 0 0 0 4px var(--dcn-primary-soft);
}
.status-offline,
.status-failed {
  background: var(--dcn-danger);
}
.status-maintenance,
.status-warning {
  background: var(--dcn-warning);
}
.job-form {
  max-width: 760px;
}
.field-unit {
  margin-left: 8px;
  color: var(--dcn-text-secondary);
}
.preset-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: -4px 0 20px;
}
.preset-row button {
  padding: 7px 10px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-card);
  color: var(--dcn-text-secondary);
  cursor: pointer;
}
.preset-row button:hover {
  color: var(--dcn-primary);
  border-color: var(--dcn-primary);
}
.field-hint {
  color: var(--dcn-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.report-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.field-hint-sub {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.85;
}
.command-input :deep(textarea),
.step-row code,
.step-output pre {
  font-family: var(--dcn-font-mono);
}
.power-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  width: 100%;
}
.power-options button {
  display: grid;
  grid-template-columns: 36px 1fr;
  gap: 2px 10px;
  align-items: center;
  padding: 18px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
  color: var(--dcn-text-primary);
  text-align: left;
  cursor: pointer;
}
.power-options button .el-icon {
  grid-row: 1 / 3;
  font-size: 24px;
  color: var(--dcn-danger);
}
.power-options button small {
  color: var(--dcn-text-secondary);
}
.power-options button.selected {
  border-color: var(--dcn-danger);
  background: color-mix(in srgb, var(--dcn-danger) 8%, transparent);
}
.risk-alert {
  margin-bottom: 18px;
}
.confirm-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.confirm-grid > div {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
}
.confirm-grid span {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.selected-devices-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 16px 0;
}
.risk-confirm {
  margin-top: 8px;
}
.create-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  max-width: 1000px;
  margin: 16px auto 0;
}
.history-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.history-toolbar .el-select {
  width: 150px;
}
.job-table {
  cursor: pointer;
}
.table-job {
  display: flex;
  align-items: center;
  gap: 10px;
}
.table-job > span:last-child {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.pagination-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
.job-detail {
  min-height: 280px;
}
.detail-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.detail-question {
  margin-bottom: 16px;
  padding: 12px 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
}
.detail-question-label {
  display: block;
  margin-bottom: 6px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.detail-question p {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.target-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 18px;
}
.target-card {
  padding: 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
}
.target-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.target-head > span:nth-child(2) {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
}
.target-duration {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
/* 巡检记录还没拉到时先用 result_json 里现成的计数顶上；卡片就绪后这一条不再渲染 */
.inspection-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 12px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.inspection-summary .sum-normal {
  color: var(--dcn-success);
}
.inspection-summary .sum-warning {
  color: var(--dcn-warning);
}
.inspection-summary .sum-critical {
  color: var(--dcn-danger);
}
/* 执行明细默认收起：step.output 是命令原文，只在排障时展开看 */
.step-collapse {
  margin-top: 10px;
  border: 0;
}
.step-collapse :deep(.el-collapse-item__header) {
  height: 32px;
  border: 0;
  background: transparent;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.step-collapse :deep(.el-collapse-item__wrap) {
  border: 0;
  background: transparent;
}
.step-list {
  margin: 12px 0 0 18px;
  padding-left: 14px;
  border-left: 1px solid var(--dcn-border-light);
}
.step-row {
  display: grid;
  grid-template-columns: 18px minmax(120px, auto) auto 1fr;
  gap: 8px;
  /* 图标(18px)与文字(13px)同行对齐:start 会让图标顶格、文字下沉一格 */
  align-items: center;
  padding: 8px 0;
}
.step-ok {
  color: var(--dcn-success);
}
.step-failed {
  color: var(--dcn-danger);
}
.step-name {
  font-size: 13px;
}
.step-row code {
  color: var(--dcn-text-secondary);
  font-size: 11px;
}
.step-output {
  grid-column: 2 / -1;
  border: 0;
}
.step-output :deep(.el-collapse-item__header) {
  height: 28px;
  border: 0;
  background: transparent;
  color: var(--dcn-primary);
  font-size: 12px;
}
.step-output :deep(.el-collapse-item__wrap) {
  border: 0;
  background: transparent;
}
.step-output pre {
  max-height: 240px;
  overflow: auto;
  padding: 10px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-code);
  color: var(--dcn-text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
}
.step-output pre.error {
  color: var(--dcn-danger);
}
.agent-report {
  margin-top: 14px;
  padding: 14px;
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
  line-height: 1.65;
}
@media (max-width: 1100px) {
  .summary-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .workbench-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 760px) {
  .automation-body {
    padding-inline: 12px;
  }
  .type-grid,
  .confirm-grid,
  .power-options {
    grid-template-columns: 1fr;
  }
  .device-toolbar {
    grid-template-columns: 1fr;
  }
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
