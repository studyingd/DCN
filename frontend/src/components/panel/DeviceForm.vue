<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑设备' : '新建设备'"
    width="600px"
    @close="handleClose"
  >
    <el-form
      ref="formRef"
      :model="formData"
      :rules="formRules"
      label-width="100px"
      label-position="right"
    >
      <!-- 1. 设备名称（必填） -->
      <el-form-item label="设备名称" prop="name">
        <el-input v-model="formData.name" placeholder="请输入设备名称" />
      </el-form-item>

      <!-- 2. 设备类型：服务器、主机、交换机、路由器、防火墙（必填） -->
      <el-form-item label="设备类型" prop="type">
        <el-select v-model="formData.type" placeholder="请选择设备类型" style="width: 100%">
          <el-option label="服务器" value="server" />
          <el-option label="主机" value="host" />
          <el-option label="交换机" value="switch" />
          <el-option label="路由器" value="router" />
          <el-option label="防火墙" value="firewall" />
        </el-select>
      </el-form-item>

      <!-- 3. IP 地址（必填） -->
      <el-form-item label="IP 地址" prop="ip_address" required>
        <el-input v-model="formData.ip_address" placeholder="请输入 IP 地址" />
      </el-form-item>

      <!-- 4. 凭据绑定（必填） -->
      <el-form-item label="绑定凭据" prop="credential_id" required>
        <div style="display: flex; gap: 8px; width: 100%;">
          <el-select
            v-model="formData.credential_id"
            placeholder="选择凭据（可选）"
            clearable
            style="flex: 1"
          >
            <el-option
              v-for="c in credentialList"
              :key="c.id"
              :label="`${c.name} (${c.username})`"
              :value="c.id"
            />
          </el-select>
          <el-button size="default" @click="openCreateCredential">新建</el-button>
        </div>
      </el-form-item>

      <!-- 快速新建凭据弹窗 -->
      <el-dialog
        v-model="credentialDialogVisible"
        title="新建凭据"
        width="420px"
        :close-on-click-modal="false"
        append-to-body
      >
        <el-form
          ref="credentialFormRef"
          :model="credentialForm"
          :rules="credentialFormRules"
          label-width="80px"
        >
          <el-form-item label="名称" prop="name">
            <el-input v-model="credentialForm.name" placeholder="例：生产服务器 Root" />
          </el-form-item>
          <el-form-item label="用户名" prop="username">
            <el-input v-model="credentialForm.username" placeholder="SSH/RDP 登录用户名" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="credentialForm.password"
              type="password"
              show-password
              placeholder="请输入密码"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="credentialDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="credentialCreating" @click="handleCreateCredential">
            创建并绑定
          </el-button>
        </template>
      </el-dialog>

      <!-- 5. 操作系统（仅服务器/主机显示）+ 检测按钮 -->
      <el-form-item v-if="showOsField" label="操作系统" prop="os_system">
        <div style="display: flex; gap: 8px; width: 100%;">
          <el-select
            v-model="formData.os_system"
            placeholder="请选择操作系统"
            style="flex: 1"
            filterable
            allow-create
          >
            <el-option label="Linux" value="linux" />
            <el-option label="Windows" value="windows" />
          </el-select>
          <el-button
            type="primary"
            plain
            :loading="detecting"
            :disabled="!isValidIp(formData.ip_address)"
            @click="handleDetectOS"
          >
            {{ detecting ? '检测中...' : '检测' }}
          </el-button>
        </div>
        <!-- 检测结果提示 -->
        <div v-if="detectResult" class="detect-result" :class="`confidence-${detectResult.confidence}`">
          <span class="detect-text">{{ detectResult.detail }}</span>
          <el-tag
            v-if="detectResult.confidence === 'high'"
            size="small"
            type="success"
            effect="plain"
          >高可信度</el-tag>
          <el-tag
            v-else-if="detectResult.confidence === 'medium'"
            size="small"
            type="warning"
            effect="plain"
          >中可信度</el-tag>
          <el-tag
            v-else
            size="small"
            type="info"
            effect="plain"
          >低可信度</el-tag>
        </div>
      </el-form-item>

      <!-- 6. 业务地址（选填） -->
      <el-form-item label="业务地址" prop="web_url">
        <el-input v-model="formData.web_url" placeholder="例: http://192.168.1.1:8080" />
      </el-form-item>

      <!-- 7. 负责人（选填） -->
      <el-form-item label="负责人" prop="owner">
        <el-input v-model="formData.owner" placeholder="请输入负责人" />
      </el-form-item>

      <!-- 8. 用途（选填） -->
      <el-form-item label="用途" prop="purpose">
        <el-input
          v-model="formData.purpose"
          type="textarea"
          :rows="2"
          placeholder="请输入设备用途"
        />
      </el-form-item>

      <!-- 机柜模式：U 位信息 -->
      <template v-if="showPositionFields">
        <el-form-item label="U 位位置" prop="position_u">
          <el-input-number v-model="formData.position_u" :min="1" :max="100" />
        </el-form-item>

        <el-form-item label="占用 U 数" prop="size_u">
          <el-input-number v-model="formData.size_u" :min="1" :max="100" />
        </el-form-item>
      </template>

      <!-- 条件端口：服务器/主机 + Windows → RDP端口；其余 → SSH端口 -->
      <el-form-item v-if="showRdpPort" label="RDP 端口" prop="rdp_port">
        <el-input-number v-model="formData.rdp_port" :min="1" :max="65535" />
      </el-form-item>

      <el-form-item v-if="showSshPort" label="SSH 端口" prop="ssh_port">
        <el-input-number v-model="formData.ssh_port" :min="1" :max="65535" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">
        {{ isEdit ? '保存' : '创建' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useDeviceStore } from '@/stores/device'
import { credentialAPI, deviceAPI } from '@/api'
import type { Device, Credential } from '@/types'

const deviceStore = useDeviceStore()

const credentialList = ref<Credential[]>([])

async function loadCredentials() {
  try {
    const res = await credentialAPI.list()
    credentialList.value = res.data
  } catch { /* ignore */ }
}

// ── Inline credential creation ──
const credentialDialogVisible = ref(false)
const credentialCreating = ref(false)
const credentialFormRef = ref<FormInstance>()

interface CredentialFormData {
  name: string
  username: string
  password: string
}
const credentialForm = reactive<CredentialFormData>({ name: '', username: '', password: '' })

const credentialFormRules = reactive<FormRules<CredentialFormData>>({
  name: [{ required: true, message: '请输入凭据名称', trigger: 'blur' }],
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
})

function openCreateCredential() {
  credentialForm.name = ''
  credentialForm.username = ''
  credentialForm.password = ''
  credentialFormRef.value?.clearValidate()
  credentialDialogVisible.value = true
}

async function handleCreateCredential() {
  const valid = await credentialFormRef.value?.validate().catch(() => false)
  if (!valid) return

  credentialCreating.value = true
  try {
    const res = await credentialAPI.create({
      name: credentialForm.name,
      username: credentialForm.username,
      password: credentialForm.password,
    })
    ElMessage.success('凭据创建成功')
    // Refresh list and auto-select the new credential
    await loadCredentials()
    formData.credential_id = res.data.id
    credentialDialogVisible.value = false
  } catch {
    ElMessage.error('创建凭据失败')
  } finally {
    credentialCreating.value = false
  }
}

const props = withDefaults(
  defineProps<{
    visible: boolean
    rackId: number
    device?: Device | null
    rackType?: 'cabinet' | 'shelf'
    presetU?: number | null
  }>(),
  {
    device: null,
    rackType: 'cabinet',
    presetU: null,
  }
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const detecting = ref(false)

interface DetectResult {
  ip_address: string
  os_system: string
  os_version: string
  ssh_banner: string | null
  ssh_port_open: boolean
  rdp_port_open: boolean
  smb_port_open: boolean
  confidence: string
  detail: string
}
const detectResult = ref<DetectResult | null>(null)

function isValidIp(value: string): boolean {
  const ipRegex = /^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$/
  return ipRegex.test(value)
}

async function handleDetectOS() {
  if (!isValidIp(formData.ip_address)) return

  detecting.value = true
  detectResult.value = null
  try {
    const res = await deviceAPI.detectOS(
      formData.ip_address,
      formData.credential_id ?? undefined,
    )
    detectResult.value = res.data

    // Auto-fill the OS field with the detected version string
    // e.g. "Rocky Linux 10.0", "Windows Server 2022", "Ubuntu 22.04"
    // Falls back to os_system category if no version detail available
    const osSystem = res.data.os_system
    const osVersion = res.data.os_version

    if (osSystem === 'linux' || osSystem === 'windows') {
      // Use the detailed version string (e.g. "Rocky Linux 10.0")
      // If version is just the generic category name, keep the category
      const genericNames = new Set(['linux', 'windows', 'linux (openssh)', 'linux (dropbear)'])
      const versionLower = osVersion.toLowerCase()
      if (osVersion && !genericNames.has(versionLower)) {
        formData.os_system = osVersion
      } else {
        formData.os_system = osSystem
      }
    } else if (osSystem === 'switch' || osSystem === 'router') {
      // Network devices: keep as-is, they use SSH
      formData.os_system = ''
      // But still show the detection for reference
    }

    if (res.data.os_system !== 'unknown') {
      ElMessage.success(`检测完成: ${res.data.os_version || res.data.os_system}`)
    } else {
      ElMessage.warning('未能识别操作系统，请手动选择')
    }
  } catch {
    detectResult.value = {
      ip_address: formData.ip_address,
      os_system: 'unknown',
      os_version: '',
      ssh_banner: null,
      ssh_port_open: false,
      rdp_port_open: false,
      smb_port_open: false,
      confidence: 'low',
      detail: '检测失败，请检查 IP 地址是否可达',
    }
    ElMessage.error('操作系统检测失败')
  } finally {
    detecting.value = false
  }
}

const isEdit = computed(() => !!props.device)

const showPositionFields = computed(() => props.rackType === 'cabinet')

interface FormData {
  name: string
  type: string
  ip_address: string
  purpose: string
  os_system: string
  position_u: number | null
  size_u: number | null
  ssh_port: number
  rdp_port: number
  web_url: string
  owner: string
  credential_id: number | null
}

const defaultFormData = (): FormData => ({
  name: '',
  type: '',
  ip_address: '',
  purpose: '',
  os_system: '',
  position_u: null,
  size_u: 1,
  ssh_port: 22,
  rdp_port: 3389,
  web_url: '',
  owner: '',
  credential_id: null,
})

const formData = reactive<FormData>(defaultFormData())

// 操作系统仅对服务器/主机显示
const showOsField = computed(() => ['server', 'host'].includes(formData.type))

// isWindows computed — uses substring match like backend get_target_type()
const isWindowsOs = computed(() => {
  if (!['server', 'host'].includes(formData.type)) return false
  return (formData.os_system || '').toLowerCase().includes('windows')
})

// RDP 端口：仅服务器/主机 + Windows 时显示
const showRdpPort = computed(() => isWindowsOs.value)

// SSH 端口：非 (服务器/主机 + Windows) 即显示（即其他类型 或 服务器/主机+Linux）
const showSshPort = computed(() => !isWindowsOs.value)

// 当设备类型切换为非 server/host 时，清空 os_system
watch(
  () => formData.type,
  () => {
    if (!['server', 'host'].includes(formData.type)) {
      formData.os_system = ''
    }
  }
)

// 当 IP 地址变化时，清除旧的检测结果
watch(
  () => formData.ip_address,
  () => {
    detectResult.value = null
  }
)

// 当绑定凭据时，自动触发操作系统检测（仅新建，编辑时不触发）
watch(
  () => formData.credential_id,
  (newVal, oldVal) => {
    if (isEdit.value) return
    if (newVal == null) return
    if (newVal === oldVal) return
    if (detecting.value) return
    if (!isValidIp(formData.ip_address)) return
    if (!showOsField.value) return
    handleDetectOS()
  }
)

const validateIpAddress = (_rule: unknown, value: string, callback: (error?: Error) => void) => {
  if (!value) {
    callback(new Error('请输入 IP 地址'))
    return
  }
  const ipRegex =
    /^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$/
  if (ipRegex.test(value)) {
    callback()
  } else {
    callback(new Error('请输入有效的 IP 地址'))
  }
}

const formRules = reactive<FormRules<FormData>>({
  name: [{ required: true, message: '请输入设备名称', trigger: 'blur' }],
  type: [{ required: true, message: '请选择设备类型', trigger: 'change' }],
  ip_address: [{ required: true, validator: validateIpAddress, trigger: 'blur' }],
  credential_id: [{ required: true, message: '请选择或新建凭据', trigger: 'change' }],
})

watch(
  () => props.visible,
  (val) => {
    if (val) {
      detectResult.value = null
      loadCredentials()
      if (props.device) {
        Object.assign(formData, {
          name: props.device.name,
          type: props.device.type,
          ip_address: props.device.ip_address,
          purpose: props.device.purpose,
          os_system: props.device.os_system,
          position_u: props.device.position_u,
          size_u: props.device.size_u ?? 1,
          ssh_port: props.device.ssh_port,
          rdp_port: props.device.rdp_port,
          web_url: props.device.web_url,
          owner: props.device.owner,
          credential_id: props.device.credential_id,
        })
      } else {
        Object.assign(formData, defaultFormData())
        if (props.presetU != null) {
          formData.position_u = props.presetU
        }
      }
      formRef.value?.clearValidate()
    }
  }
)

function handleClose() {
  formRef.value?.resetFields()
  Object.assign(formData, defaultFormData())
  emit('update:visible', false)
}

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const payload: Partial<Device> = {
      name: formData.name,
      type: formData.type,
      ip_address: formData.ip_address,
      purpose: formData.purpose,
      os_system: formData.os_system,
      ssh_port: formData.ssh_port,
      rdp_port: formData.rdp_port,
      web_url: formData.web_url || null,
      owner: formData.owner || null,
      credential_id: formData.credential_id,
    }

    if (showPositionFields.value) {
      payload.position_u = formData.position_u
      payload.size_u = formData.size_u
    } else {
      payload.position_u = null
      payload.size_u = null
    }

    if (isEdit.value && props.device) {
      await deviceStore.updateDevice(props.device.id, payload)
      ElMessage.success('设备更新成功')
    } else {
      await deviceStore.createDevice(props.rackId, payload)
      ElMessage.success('设备创建成功')
    }

    emit('saved')
    handleClose()
  } catch {
    ElMessage.error(isEdit.value ? '更新设备失败' : '创建设备失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
/* ── OS Detection result indicator ── */
.detect-result {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.4;
}

.detect-result .detect-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.detect-result .detect-text {
  flex: 1;
  color: #606266;
}

.detect-result.confidence-high {
  background: #f0f9eb;
  border: 1px solid #c2e7b0;
}

.detect-result.confidence-high .detect-text {
  color: #3d8b37;
}

.detect-result.confidence-medium {
  background: #fdf6ec;
  border: 1px solid #f5dab1;
}

.detect-result.confidence-medium .detect-text {
  color: #b88230;
}

.detect-result.confidence-low {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
}

.detect-result.confidence-low .detect-text {
  color: #909399;
}
</style>
