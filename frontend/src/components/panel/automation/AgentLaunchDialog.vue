<template>
  <el-dialog v-model="visible" title="Agent 分析诊断" width="920px" :close-on-click-modal="false" class="launch-dialog">
    <el-steps :active="step" align-center finish-status="success" class="launch-steps">
      <el-step title="选择目标" /><el-step title="诊断配置" />
    </el-steps>

    <!-- ── 第 1 步:选择目标(与健康巡检同款分组选择器;单台约束=选中后其它置灰) ── -->
    <template v-if="step === 0">
      <el-alert v-if="devicesError" type="error" :closable="false" show-icon class="devices-error">
        <template #title>设备候选加载失败</template>
        <el-button size="small" type="primary" plain @click="loadDevices">重试</el-button>
      </el-alert>
      <template v-else>
        <div class="step-heading">
          <strong>选择诊断目标</strong>
          <span>一次诊断针对一台设备(服务器/PVE 虚机);选定后其它候选置灰,点击已选目标可重新选择</span>
        </div>
        <div class="device-toolbar">
          <span class="selected-counter">{{ chosenDevice ? `已选:${chosenDevice.name}` : '未选择目标' }}</span>
          <el-button v-if="chosenId != null" link type="danger" @click="chosenId = null">清除</el-button>
        </div>
        <DeviceSelector :selected-ids="selectedIds" :selectable-fn="selectable" @confirm="onSelectorConfirm" />
      </template>
    </template>

    <!-- ── 第 2 步:诊断配置(问题描述;动作模式已删,写动作全审批) ── -->
    <template v-else>
      <div class="step-heading">
        <strong>诊断配置</strong>
        <span>Agent 只读取证并给出结论报告;若提议变更动作,均需人工审批后才会执行</span>
      </div>
      <div v-if="chosenDevice" class="target-summary">
        <span class="target-label">诊断目标</span>
        <strong>{{ chosenDevice.name }}</strong>
        <small>
          {{ chosenDevice.ip_address || '未绑定 IP'
          }}{{ chosenDevice.kind === 'pve_guest' ? '' : ` · ${chosenDevice.os_system || '—'}` }}
        </small>
      </div>
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="问题描述">
          <el-input
            v-model="question"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="例如:这台机器内存占用为什么这么高?/web 容器为什么反复重启?"
            :disabled="starting"
            @keydown.ctrl.enter="start()"
          />
        </el-form-item>
        <div class="atm-preset-row preset-row-compact">
          <button v-for="p in presets" :key="p" type="button" @click="question = p">{{ p }}</button>
        </div>
      </el-form>
    </template>

    <template #footer>
      <el-button v-if="step === 0" @click="visible = false">取消</el-button>
      <el-button v-if="step === 0" type="primary" :disabled="chosenId == null" @click="step = 1">下一步</el-button>
      <template v-else>
        <el-button @click="step = 0">上一步</el-button>
        <el-button type="primary" :loading="starting" :disabled="!canStart" @click="start()">开始诊断</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { agentAPI } from '@/api'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import type { AgentDevice } from '@/types/agent'
import './automation.css'

/**
 * Agent 分析诊断发起弹窗(两步,与健康巡检同款交互:先选目标、再配置问题)。
 * - 第 1 步用公共 DeviceSelector(机房/虚拟化分组卡片);单台约束靠
 *   selectableFn——选中一台后其它候选置灰(点击已选目标可取消),比
 *   「多选后悄悄截断第一台」诚实。
 * - 第 2 步:问题描述。动作模式已删(2026-10-30):后端固定 strict,
 *   白名单只读取证自动、写动作全人工审批,发起人不再选择。
 * open() 每次重置;开始后 emit('started', runId) 交给父壳打开时间线抽屉。
 */
const emit = defineEmits<{ (e: 'started', runId: number): void }>()

const visible = ref(false)
const step = ref(0)
const devices = ref<AgentDevice[]>([])
const devicesError = ref(false)
const devicesLoading = ref(false)
const chosenId = ref<number | null>(null)
const question = ref('')
const starting = ref(false)

const presets = [
  '这台机器 CPU 占用为什么这么高?',
  '内存占用持续偏高,是否有泄漏?',
  'web 容器为什么反复重启?',
  '全面体检:检查系统健康状态并给出建议',
]

const chosenDevice = computed(() => devices.value.find((d) => d.id === chosenId.value) || null)
const selectedIds = computed(() => (chosenId.value != null ? [chosenId.value] : []))

/**
 * 诊断都是连进目标内部执行(SSH/WinRM),与批量任务的非 power 口径一致:
 * 需要运维接入凭据且已绑定 IP(PVE guest 无绑定 IP 时后端 diagnose 也会 400,
 * 在这里提前拦住比提交后报错友好)。
 */
function baseSelectable(id: number): boolean {
  const device = devices.value.find((d) => d.id === id)
  return !!device && !!device.has_credential && !!device.ip_address
}
/** 单台约束:选中一台后其它候选不可再选,已选的保持可点(用于取消) */
const selectable = (id: number) => baseSelectable(id) && (chosenId.value == null || chosenId.value === id)

const canStart = computed(() => chosenId.value != null && question.value.trim().length >= 2)

async function loadDevices() {
  devicesLoading.value = true
  devicesError.value = false
  try {
    devices.value = (await agentAPI.devices()).data
  } catch {
    // 静默吞掉只会让候选恒空且无解释,显式给错误态 + 重试
    devicesError.value = true
  } finally {
    devicesLoading.value = false
  }
}

async function open() {
  step.value = 0
  chosenId.value = null
  question.value = ''
  visible.value = true
  await loadDevices()
}

function onSelectorConfirm(ids: number[]) {
  // 单台约束下 ids 至多 1 个;防御式兜底取最后一个
  chosenId.value = ids.length ? ids[ids.length - 1] : null
}

async function start() {
  if (!canStart.value || chosenId.value == null) return
  starting.value = true
  try {
    const res = await agentAPI.diagnose(chosenId.value, question.value.trim())
    ElMessage.success('诊断已启动')
    visible.value = false
    emit('started', res.data.run_id)
  } catch (e) {
    ElMessage.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '启动失败')
  } finally {
    starting.value = false
  }
}

defineExpose({ open })
</script>

<style scoped>
.launch-dialog :deep(.el-dialog__body) {
  max-height: calc(100vh - 240px);
  overflow: auto;
}
.launch-steps {
  max-width: 480px;
  margin: 0 auto 18px;
}
.step-heading {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 16px;
}
.step-heading strong {
  font-size: 18px;
}
.step-heading span {
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.devices-error {
  margin-bottom: 12px;
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
.target-summary {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 16px;
  padding: 12px 14px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
}
.target-summary .target-label {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.target-summary small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.preset-row-compact {
  margin: -8px 0 8px;
}
</style>
