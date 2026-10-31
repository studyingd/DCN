<template>
  <section class="automation-page schedules-page">
    <div class="atm-toolbar">
      <el-button :loading="loading" @click="load"
        ><el-icon><Refresh /></el-icon>刷新</el-button
      >
    </div>
    <el-table v-loading="loading" :data="schedules" row-key="id">
      <el-table-column prop="name" label="计划名称" min-width="180" show-overflow-tooltip />
      <el-table-column label="任务类型" width="150"
        ><template #default="{ row }">{{
          jobTypeMeta[row.job_type as AutomationJobType].label
        }}</template></el-table-column
      >
      <el-table-column label="目标" width="90" align="center"
        ><template #default="{ row }">{{ row.target_ids.length }} 台</template></el-table-column
      >
      <el-table-column label="执行周期" min-width="180"
        ><template #default="{ row }">
          <span v-if="row.schedule_type === 'once'">{{ formatDateTime(row.scheduled_at) }}</span>
          <!-- 列表里显示中文周期，原始表达式放到 title 里备查 -->
          <span v-else :title="row.cron_expression || ''">{{ humanizeCron(row.cron_expression) }}</span>
        </template></el-table-column
      >
      <el-table-column label="下次执行" width="180"
        ><template #default="{ row }">{{ row.next_run_at ? formatDateTime(row.next_run_at) : '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100" align="center"
        ><template #default="{ row }"
          ><el-tag :type="scheduleStatusTagType(row.status)" effect="plain">{{
            scheduleStatusLabel(row.status)
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="操作" width="170" align="center"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="openEdit(row)">编辑</el-button
          ><el-button v-if="row.status === 'active'" link type="warning" @click="pause(row)">暂停</el-button
          ><el-button v-if="row.status === 'paused'" link type="success" @click="resume(row)">启用</el-button
          ><el-button link type="danger" @click="remove(row)">删除</el-button></template
        ></el-table-column
      >
    </el-table>
    <el-empty
      v-if="!loading && schedules.length === 0"
      description="暂无定时计划，可在新建任务时选择定时或周期执行"
      :image-size="64"
    />

    <!-- 计划编辑弹窗:名称/目标/配置/周期。与创建向导同组件(DeviceSelector/CronSchedulePicker),
         预填现值,提交走 PUT;job_type 不可改(后端口径一致)。 -->
    <el-dialog
      v-model="editVisible"
      :title="`编辑计划 · ${editing?.name || ''}`"
      width="720px"
      :close-on-click-modal="false"
    >
      <div v-loading="devicesLoading" class="schedule-edit-body">
        <el-form label-position="top" class="job-form">
          <el-form-item label="计划名称" required
            ><el-input v-model="editForm.name" maxlength="255" show-word-limit
          /></el-form-item>
          <el-form-item label="目标"
            ><span class="atm-field-hint">已选 {{ editForm.deviceIds.length }} 个目标</span>
            <DeviceSelector :selected-ids="editForm.deviceIds" :selectable-fn="selectable" @confirm="onSelectorConfirm"
          /></el-form-item>
          <template v-if="editing?.job_type === 'agent'">
            <el-form-item label="诊断问题" required
              ><el-input v-model="editForm.question" type="textarea" :rows="4" maxlength="500" show-word-limit
            /></el-form-item>
          </template>
          <template v-else-if="editing?.job_type === 'inspection'">
            <el-form-item label="单项超时(秒)"
              ><el-input-number v-model="editForm.timeout" :controls="false" :min="5" :max="300"
            /></el-form-item>
          </template>
          <template v-else-if="editing?.job_type === 'script'">
            <el-form-item label="执行命令" required
              ><el-input
                v-model="editForm.command"
                type="textarea"
                :rows="4"
                placeholder="输入需要在目标设备上执行的命令"
                class="command-input"
            /></el-form-item>
            <el-form-item label="执行超时(秒)"
              ><el-input-number v-model="editForm.timeout" :controls="false" :min="5" :max="300"
            /></el-form-item>
          </template>
          <template v-else>
            <el-form-item label="电源操作" required>
              <el-radio-group v-model="editForm.powerAction">
                <el-radio-button value="reboot">重启</el-radio-button>
                <el-radio-button value="shutdown">关机</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </template>
          <el-form-item label="执行周期" required
            ><CronSchedulePicker v-model="editForm.cronExpression"
          /></el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!canSave" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, toRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { automationAPI } from '@/api'
import { deviceSelectable } from '@/composables/useAutomationDevices'
import { scheduleStatusLabel, scheduleStatusTagType } from '@/utils/agentRunText'
import { humanizeCron, isCronShape } from '@/utils/cron'
import { formatDateTime } from '@/utils/datetime'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import CronSchedulePicker from '../CronSchedulePicker.vue'
import { jobTypeMeta } from './jobTypes'
import type { AutomationDevice, AutomationJobType, AutomationSchedule } from '@/types/automation'
import './automation.css'

const props = defineProps<{
  devices: AutomationDevice[]
  devicesLoading: boolean
  /** 懒加载设备列表(编辑弹窗打开时由父壳拉取) */
  ensureDevices: () => void
}>()

const schedules = ref<AutomationSchedule[]>([])
const loading = ref(false)

const editVisible = ref(false)
const saving = ref(false)
const editing = ref<AutomationSchedule | null>(null)
const editForm = reactive({
  name: '',
  deviceIds: [] as number[],
  question: '',
  command: '',
  timeout: 30,
  powerAction: 'reboot' as 'reboot' | 'shutdown',
  cronExpression: '',
})

// 可选规则按**被编辑计划**的任务类型判断(电源走 PVE API 无需 guest 凭据);
// 旧实现误读创建向导的 jobType,导致编辑电源计划时虚机目标被误禁
const selectable = deviceSelectable(toRef(props, 'devices'), () => editing.value?.job_type ?? 'inspection')

const canSave = computed(() => {
  if (!editForm.name.trim()) return false
  if (!editForm.deviceIds.length) return false
  if (!isCronShape(editForm.cronExpression)) return false
  if (editing.value?.job_type === 'agent' && editForm.question.trim().length < 2) return false
  if (editing.value?.job_type === 'script' && !editForm.command.trim()) return false
  return true
})

async function load() {
  loading.value = true
  try {
    schedules.value = (await automationAPI.schedules()).data
  } catch {
    ElMessage.error('加载定时计划失败')
  } finally {
    loading.value = false
  }
}

function openEdit(schedule: AutomationSchedule) {
  editing.value = schedule
  editForm.name = schedule.name
  editForm.deviceIds = [...(schedule.target_ids || [])]
  editForm.question = String(schedule.config_json?.question || '')
  editForm.command = String(schedule.config_json?.command || '')
  editForm.timeout = Number(schedule.config_json?.timeout || 30)
  editForm.powerAction = (schedule.config_json?.action as 'reboot' | 'shutdown') || 'reboot'
  editForm.cronExpression = schedule.cron_expression || ''
  props.ensureDevices() // 编辑也要选目标,懒加载设备列表
  editVisible.value = true
}

function onSelectorConfirm(ids: number[]) {
  editForm.deviceIds = ids
}

async function save() {
  if (!editing.value || !canSave.value) return
  saving.value = true
  try {
    const jt = editing.value.job_type
    const config: Record<string, any> =
      jt === 'agent'
        ? { question: editForm.question }
        : jt === 'inspection'
          ? { mode: 'core', timeout: editForm.timeout }
          : jt === 'script'
            ? { command: editForm.command, timeout: editForm.timeout }
            : { action: editForm.powerAction }
    await automationAPI.updateSchedule(editing.value.id, {
      name: editForm.name.trim(),
      device_ids: editForm.deviceIds,
      config,
      schedule_type: 'recurring',
      cron_expression: editForm.cronExpression,
    })
    ElMessage.success('计划已更新,下次执行时间已按新周期重算')
    editVisible.value = false
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '保存计划失败')
  } finally {
    saving.value = false
  }
}

async function pause(schedule: AutomationSchedule) {
  try {
    await automationAPI.setScheduleStatus(schedule.id, 'paused')
    ElMessage.success('计划已暂停,到期不再触发')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '暂停失败')
  }
}

async function resume(schedule: AutomationSchedule) {
  try {
    await automationAPI.setScheduleStatus(schedule.id, 'active')
    ElMessage.success('计划已恢复,按周期继续执行')
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '恢复失败')
  }
}

async function remove(schedule: AutomationSchedule) {
  try {
    await ElMessageBox.confirm(`确定删除计划「${schedule.name}」吗？`, '删除计划', { type: 'warning' })
    await automationAPI.deleteSchedule(schedule.id)
    ElMessage.success('计划已删除')
    await load()
  } catch {
    /* cancelled */
  }
}

onMounted(load)
</script>

<style scoped>
.job-form {
  max-width: 760px;
}
.command-input :deep(textarea) {
  font-family: var(--dcn-font-mono);
}
</style>
