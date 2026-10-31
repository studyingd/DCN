<template>
  <el-dialog v-model="visible" title="新建任务" width="920px" :close-on-click-modal="false" class="wizard-dialog">
    <el-steps :active="createStep" align-center finish-status="success" class="create-steps">
      <el-step title="任务类型" /><el-step title="目标设备" /><el-step title="任务配置" /><el-step title="确认执行" />
    </el-steps>

    <div class="create-surface">
      <template v-if="createStep === 0">
        <div class="step-heading"><strong>选择任务类型</strong><span>不同类型具有独立的权限和安全边界</span></div>
        <div class="atm-type-grid create-type-grid">
          <button
            v-for="type in types"
            :key="type.key"
            type="button"
            class="atm-type-card"
            :class="{ selected: form.jobType === type.key }"
            @click="form.jobType = type.key"
          >
            <span class="atm-icon-box atm-type-icon" :class="`atm-icon--${type.tone}`"
              ><el-icon><component :is="type.icon" /></el-icon
            ></span>
            <span class="atm-type-copy"
              ><strong>{{ type.label }}</strong
              ><small>{{ type.description }}</small></span
            >
            <el-icon v-if="form.jobType === type.key" class="selected-check"><CircleCheckFilled /></el-icon>
          </button>
        </div>
      </template>

      <template v-else-if="createStep === 1">
        <div class="step-heading"><strong>选择目标</strong><span>可选择多台设备/虚拟机批量执行</span></div>
        <div class="device-toolbar">
          <span class="selected-counter">已选择 {{ form.deviceIds.length }} 个目标</span>
          <el-button v-if="form.deviceIds.length" link type="danger" @click="form.deviceIds = []">清空</el-button>
        </div>
        <!-- 目标选择器内联展示:机房设备 + 虚拟机,勾选即生效 -->
        <DeviceSelector :selected-ids="form.deviceIds" :selectable-fn="selectable" @confirm="onSelectorConfirm" />
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

          <template v-if="form.jobType === 'inspection'">
            <!-- 检查项由后端按设备操作系统自动决定，这里不再让用户挑模式 / 勾选项 -->
            <el-form-item label="检查内容">
              <span class="atm-field-hint">自动检查 CPU、内存、磁盘等 6 项核心指标，按设备操作系统自动适配。</span>
            </el-form-item>
            <el-form-item label="单项超时"
              ><el-input-number v-model="form.timeout" :controls="false" :min="5" :max="300" /><span class="field-unit"
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
            <div class="atm-preset-row">
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
              ><el-input-number v-model="form.timeout" :controls="false" :min="5" :max="300" /><span class="field-unit"
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
                  <el-icon><RefreshRight /></el-icon><strong>重启设备</strong><small>设备将短暂离线后重新启动</small>
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
          <!-- 只有健康巡检会推 Webhook 通知(摘要 + PDF 报告);脚本/电源不发。 -->
          <el-form-item v-if="form.jobType === 'inspection'" label="Webhook 通知">
            <el-switch v-model="form.webhookNotify" />
          </el-form-item>
        </el-form>
      </template>

      <template v-else>
        <div class="step-heading"><strong>确认任务信息</strong><span>执行后可在任务详情中持续查看进度</span></div>
        <el-alert :type="currentJobMeta.readOnly ? 'success' : 'warning'" :closable="false" show-icon class="risk-alert"
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

    <template #footer>
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
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, toRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheckFilled, RefreshRight, SwitchButton } from '@element-plus/icons-vue'
import { automationAPI } from '@/api'
import { deviceSelectable } from '@/composables/useAutomationDevices'
import { isCronShape } from '@/utils/cron'
import { toUtcIso } from '@/utils/datetime'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import CronSchedulePicker from '../CronSchedulePicker.vue'
import { jobTypeMeta, type JobTypeMeta } from './jobTypes'
import type { AutomationDevice, AutomationJobType } from '@/types/automation'
import './automation.css'

/**
 * 新建任务向导弹窗:open(type?) 打开——概览类型卡传入类型时跳过第 1 步,
 * 顶栏「新建任务」不带类型从选类型开始(2026-10-27 用户拍板:创建不再
 * 占独立页签,发起统一在运维概览)。每次 open 都重置表单。
 */
const props = defineProps<{
  devices: AutomationDevice[]
  devicesLoading: boolean
  types: JobTypeMeta[]
}>()

const emit = defineEmits<{
  (e: 'job-created', jobId: number): void
  (e: 'schedule-created'): void
}>()

const visible = ref(false)
const createStep = ref(0)
const creating = ref(false)
const riskConfirmed = ref(false)
const form = reactive({
  jobType: 'inspection' as AutomationJobType,
  name: '',
  deviceIds: [] as number[],
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
    if (form.jobType === 'script' && !form.command.trim()) return false
    if (form.executionMode === 'once') return !!form.scheduledAt
    // 高级模式下可能手写了非法 cron，在这里拦住，不要等后端报错
    if (form.executionMode === 'recurring') return isCronShape(form.cronExpression)
    return true
  }
  return true
})

const selectedDevices = computed(() => props.devices.filter((device) => form.deviceIds.includes(device.id)))
const selectable = deviceSelectable(toRef(props, 'devices'), () => form.jobType)

function resetForm(jobType?: AutomationJobType) {
  form.jobType = jobType || props.types[0]?.key || 'inspection'
  form.name = ''
  form.deviceIds = []
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

function open(jobType?: AutomationJobType) {
  resetForm(jobType)
  visible.value = true
}

function onSelectorConfirm(ids: number[]) {
  form.deviceIds = ids
}
function removeTarget(id: number) {
  form.deviceIds = form.deviceIds.filter((x) => x !== id)
}
function nextStep() {
  if (!canNext.value) return
  createStep.value++
}

async function createJob() {
  creating.value = true
  try {
    const config: Record<string, any> =
      form.jobType === 'inspection'
        ? // 巡检项由后端 core 模式按设备操作系统自动决定，前端不再传 items
          { mode: 'core', timeout: form.timeout, webhook_notify: form.webhookNotify }
        : form.jobType === 'script'
          ? { command: form.command, timeout: form.timeout }
          : { action: form.powerAction }
    if (form.executionMode === 'now') {
      const res = await automationAPI.createJob({
        name: form.name,
        job_type: form.jobType,
        device_ids: form.deviceIds,
        config,
      })
      ElMessage.success('任务已创建，正在后台执行')
      visible.value = false
      emit('job-created', res.data.id)
    } else {
      await automationAPI.createSchedule({
        name: form.name,
        job_type: form.jobType,
        device_ids: form.deviceIds,
        config,
        schedule_type: form.executionMode,
        scheduled_at: toUtcIso(form.scheduledAt) || undefined,
        cron_expression: form.cronExpression || undefined,
      })
      ElMessage.success('定时计划已创建')
      visible.value = false
      emit('schedule-created')
    }
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '创建任务失败')
  } finally {
    creating.value = false
  }
}

// 换任务类型清空目标:可选规则随类型变化(power 无需凭据,其余需要)
watch(
  () => form.jobType,
  () => {
    form.deviceIds = []
    riskConfirmed.value = false
  },
)

defineExpose({ open })
</script>

<style scoped>
/* 向导内容较长(DeviceSelector + 表单),弹窗体限高滚动,步骤条跟随滚动 */
.wizard-dialog :deep(.el-dialog__body) {
  max-height: calc(100vh - 240px);
  overflow: auto;
}
.create-steps {
  max-width: 760px;
  margin: 0 auto 18px;
}
.create-surface {
  min-height: 320px;
  padding: 20px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
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
  display: flex;
  justify-content: space-between;
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
.selected-check {
  color: var(--dcn-primary);
  font-size: 19px;
}
.job-form {
  max-width: 760px;
}
.field-unit {
  margin-left: 8px;
  color: var(--dcn-text-secondary);
}
.command-input :deep(textarea) {
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
  font: inherit;
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
@media (max-width: 760px) {
  .confirm-grid,
  .power-options {
    grid-template-columns: 1fr;
  }
}
</style>
