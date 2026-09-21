<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑设备' : '新建设备'"
    width="640px"
    class="device-form-dialog"
    @close="handleClose"
  >
    <el-form ref="formRef" :model="formData" :rules="formRules" label-width="100px" label-position="right">
      <!-- 1. 设备名称（必填） -->
      <el-form-item label="设备名称" prop="name">
        <el-input v-model="formData.name" placeholder="请输入设备名称" />
      </el-form-item>

      <!-- 2. 设备类型：服务器、云服务器、台式主机（必填）。
           交换机/路由器/防火墙已彻底下线，前后端均不再保留这三种设备类型。 -->
      <el-form-item label="设备类型" prop="type">
        <el-select v-model="formData.type" placeholder="请选择设备类型" style="width: 100%" @change="scheduleOsDetect">
          <el-option label="服务器" value="server" />
          <el-option label="云服务器" value="cloud_server" />
          <el-option label="台式主机" value="host" />
        </el-select>
      </el-form-item>

      <!-- 3. IP 地址（必填） -->
      <el-form-item label="IP 地址" prop="ip_address" required>
        <el-input v-model="formData.ip_address" placeholder="请输入 IP 地址" @input="scheduleOsDetect" />
        <div class="field-help">
          输入 IP 后自动探测操作系统：Linux 显示 SSH 端口，Windows 显示 RDP / WinRM 端口；探测失败不影响设备创建。
        </div>
        <div v-if="detecting" class="detect-status">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span>正在探测操作系统…</span>
        </div>
        <div v-else-if="osLabel" class="detect-status">已识别：{{ osLabel }}</div>
        <div v-else-if="detectUnidentified" class="detect-status is-muted">未能识别操作系统，可手动填写端口</div>
        <div v-if="detectResult?.os_system === 'windows' && !detectResult.winrm_port_open" class="winrm-tools">
          <el-button size="small" type="warning" plain @click="downloadWinrmScript"> 下载 WinRM 启用脚本 </el-button>
        </div>
        <el-alert
          v-if="detectResult?.os_system === 'windows' && !detectResult.winrm_port_open"
          class="winrm-alert"
          type="warning"
          :closable="false"
          show-icon
          title="检测到 Windows，但 WinRM 端口未响应。可下载仅允许当前 DCN 出口 IP 访问的动态脚本，在目标机管理员 PowerShell 中运行。"
        />
      </el-form-item>

      <section class="form-section" aria-labelledby="remote-connection-title">
        <div class="section-heading">
          <div>
            <h3 id="remote-connection-title">远程连接</h3>
            <p>保存设备时自动加密并绑定连接信息，密码不会在页面中回显。</p>
          </div>
        </div>

        <el-form-item label="用户名" prop="remote_username">
          <el-input
            v-model="formData.remote_username"
            autocomplete="username"
            placeholder="选填，不填写则不配置远程连接"
          />
        </el-form-item>
        <el-form-item label="密码" prop="remote_password">
          <el-input
            v-model="formData.remote_password"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="
              isEdit && props.device?.has_credential ? '选填，留空则保持已有密码' : '选填，请与用户名一起填写'
            "
          />
          <div class="field-help">
            {{
              isEdit && props.device?.has_credential
                ? '清空用户名和密码并保存，将取消当前远程连接配置。'
                : '用户名和密码均不填写时，不会创建远程连接配置。'
            }}
          </div>
        </el-form-item>

        <el-form-item v-if="showSshPort" label="SSH 端口" prop="ssh_port">
          <el-input v-model.number="formData.ssh_port" inputmode="numeric" placeholder="22" />
        </el-form-item>

        <el-form-item v-if="showWindowsPorts" label="RDP 端口" prop="rdp_port">
          <el-input v-model.number="formData.rdp_port" inputmode="numeric" placeholder="3389" />
        </el-form-item>

        <el-form-item v-if="showWindowsPorts" label="WinRM 端口" prop="winrm_port">
          <el-input v-model.number="formData.winrm_port" inputmode="numeric" placeholder="5985" />
        </el-form-item>
      </section>
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
import { Loading } from '@element-plus/icons-vue'
import type { FormInstance, FormRules } from 'element-plus'
import { useDeviceStore } from '@/stores/device'
import { deviceAPI } from '@/api'
import type { Device, DeviceSavePayload } from '@/types'
import { isOpsDevice } from '@/utils/deviceLabels'
import { extractErrorDetail, readHeader } from '@/utils/apiError'

const deviceStore = useDeviceStore()

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
  },
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const detecting = ref(false)
const detectResult = ref<DetectResult | null>(null)
// 探测到的操作系统族，决定表单里显示 SSH 还是 RDP / WinRM 端口
const detectedFamily = ref<'linux' | 'windows' | null>(null)
const detectedLabel = ref<string | null>(null)
const detectUnidentified = ref(false)

interface DetectResult {
  ip_address: string
  os_system: string
  os_version: string
  ssh_banner: string | null
  ssh_port_open: boolean
  winrm_port_open: boolean
  rdp_port_open: boolean
  smb_port_open: boolean
  confidence: string
  detail: string
}

function isValidIp(value: string): boolean {
  const ipRegex = /^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$/
  return ipRegex.test(value)
}

function normalizeDetectedOs(result: DetectResult): string | null {
  const osSystem = result.os_system?.trim().toLowerCase()
  if (osSystem !== 'linux' && osSystem !== 'windows') return null

  const osVersion = result.os_version?.trim()
  const versionLower = osVersion?.toLowerCase() ?? ''
  const isGenericVersion =
    versionLower === 'linux' ||
    versionLower === 'windows' ||
    versionLower.startsWith('linux (openssh)') ||
    versionLower.startsWith('linux (dropbear)')
  if (osVersion && !isGenericVersion) return osVersion
  return osSystem
}

const LINUX_DISTRO_HINTS = [
  'linux',
  'ubuntu',
  'debian',
  'centos',
  'rocky',
  'almalinux',
  'red hat',
  'rhel',
  'fedora',
  'suse',
  'arch',
  'alpine',
  'gentoo',
  'anolis',
  'kylin',
  'uos',
  'deepin',
  'tencentos',
  'opencloudos',
  'uniontech',
  '统信',
  '麒麟',
]

// 设备里保存的 os_system 可能是精确版本串（如 "Rocky Linux 10.0"），
// 统一归一到 linux / windows 两个族，用于决定端口字段的显隐。
function osFamily(value?: string | null): 'linux' | 'windows' | null {
  const normalized = value?.trim().toLowerCase() ?? ''
  if (!normalized) return null
  if (normalized.includes('windows')) return 'windows'
  if (LINUX_DISTRO_HINTS.some((hint) => normalized.includes(hint))) return 'linux'
  return null
}

function applyDetectResult(result: DetectResult | null) {
  detectResult.value = result
  const family = result ? osFamily(result.os_system) : null
  detectedFamily.value = family
  detectedLabel.value = result && family ? (normalizeDetectedOs(result) ?? result.os_system) : null
}

function requestOsDetect(ipAddress: string) {
  return deviceAPI.detectOS(ipAddress, {
    deviceId: props.device?.id,
    username: formData.remote_username || undefined,
    password: formData.remote_password || undefined,
    winrmPort: formData.winrm_port,
  })
}

async function detectOperatingSystem(): Promise<string | null> {
  if (!supportsWindowsRemote.value || !isValidIp(formData.ip_address)) return null

  // 同一 IP 的自动探测结果可直接复用(含失败:重试大概率同样失败,且
  // 失败不影响保存——os_system 回退旧值)。避免点保存再同步探测一次
  // 四个端口,把保存按钮卡住好几秒。
  if (detectResult.value && detectResult.value.ip_address === formData.ip_address && detectedFamily.value) {
    return normalizeDetectedOs(detectResult.value)
  }

  try {
    const res = await requestOsDetect(formData.ip_address)
    applyDetectResult(res.data)
    return normalizeDetectedOs(res.data)
  } catch {
    return null
  }
}

// IP 输入停顿后自动探测，取代原来的「检测操作系统 / WinRM」按钮。
let osDetectTimer: ReturnType<typeof setTimeout> | null = null
let osDetectSeq = 0

function scheduleOsDetect() {
  if (osDetectTimer) clearTimeout(osDetectTimer)
  osDetectTimer = setTimeout(() => {
    osDetectTimer = null
    void autoDetectOs()
  }, 600)
}

function cancelOsDetect() {
  if (osDetectTimer) {
    clearTimeout(osDetectTimer)
    osDetectTimer = null
  }
  osDetectSeq += 1
  detecting.value = false
}

async function autoDetectOs() {
  if (!props.visible) return
  if (!supportsWindowsRemote.value || !isValidIp(formData.ip_address)) {
    applyDetectResult(null)
    detectUnidentified.value = false
    return
  }

  const ipAddress = formData.ip_address
  const seq = (osDetectSeq += 1)
  detecting.value = true
  try {
    const res = await requestOsDetect(ipAddress)
    // 输入已变化或对话框已关闭时丢弃过期结果
    if (seq !== osDetectSeq || ipAddress !== formData.ip_address) return
    applyDetectResult(res.data)
    detectUnidentified.value = !detectedFamily.value
  } catch {
    if (seq === osDetectSeq && ipAddress === formData.ip_address) {
      applyDetectResult(null)
      detectUnidentified.value = true
    }
  } finally {
    if (seq === osDetectSeq) detecting.value = false
  }
}

async function downloadWinrmScript() {
  if (!isValidIp(formData.ip_address)) return
  try {
    const res = await deviceAPI.downloadWinrmSetupScript(formData.ip_address, formData.winrm_port)
    const url = URL.createObjectURL(res.data)
    const link = document.createElement('a')
    link.href = url
    link.download = 'dcn-enable-winrm.ps1'
    link.click()
    URL.revokeObjectURL(url)
    const sourceIp = readHeader(res.headers, 'X-DCN-WinRM-Source-IP')
    ElMessage.success(sourceIp ? `WinRM 启用脚本已下载（仅放行 DCN 出口 IP ${sourceIp}）` : 'WinRM 启用脚本已下载')
  } catch (error: unknown) {
    // 后端 detail 里是真实原因（目标 IP 无效 / IPv6 / 无法确定出口 IP），
    // 不能统一说成「请检查目标 IP 和网络路由」。blob 错误体需先读成文本。
    ElMessage.error(await extractErrorDetail(error, '脚本下载失败'))
  }
}

const isEdit = computed(() => !!props.device)

interface FormData {
  name: string
  type: string
  ip_address: string
  ssh_port: number
  rdp_port: number
  winrm_port: number
  remote_username: string
  remote_password: string
}

const defaultFormData = (): FormData => ({
  name: '',
  type: '',
  ip_address: '',
  ssh_port: 22,
  rdp_port: 3389,
  winrm_port: 5985,
  remote_username: '',
  remote_password: '',
})

const formData = reactive<FormData>(defaultFormData())

// 纳管的三种类型(含云服务器)都可能是 Windows,都需要 RDP/WinRM 端口与 OS 探测。
// 过去写死 ['server','host'] 会让云服务器跳过探测、os_system 为空,
// 进而让监控/巡检/自动化错选 SSH 通道。
const supportsWindowsRemote = computed(() => isOpsDevice(formData.type))

// Linux 只需 SSH；Windows 只需 RDP / WinRM；未识别时三者都保留，方便手工填写。
const showSshPort = computed(() => !supportsWindowsRemote.value || detectedFamily.value !== 'windows')
const showWindowsPorts = computed(() => supportsWindowsRemote.value && detectedFamily.value !== 'linux')
const osLabel = computed(() => {
  const label = detectedLabel.value?.trim() ?? ''
  if (label.toLowerCase() === 'linux') return 'Linux'
  if (label.toLowerCase() === 'windows') return 'Windows'
  return label
})

const validateIpAddress = (_rule: unknown, value: string, callback: (error?: Error) => void) => {
  if (!value) {
    callback(new Error('请输入 IP 地址'))
    return
  }
  const ipRegex = /^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$/
  if (ipRegex.test(value)) {
    callback()
  } else {
    callback(new Error('请输入有效的 IP 地址'))
  }
}

const validatePort = (_rule: unknown, value: number, callback: (error?: Error) => void) => {
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    callback(new Error('请输入 1-65535 之间的端口号'))
    return
  }
  callback()
}

const formRules = reactive<FormRules<FormData>>({
  name: [{ required: true, message: '请输入设备名称', trigger: 'blur' }],
  type: [{ required: true, message: '请选择设备类型', trigger: 'change' }],
  ip_address: [{ required: true, validator: validateIpAddress, trigger: 'blur' }],
  ssh_port: [{ validator: validatePort, trigger: 'blur' }],
  rdp_port: [{ validator: validatePort, trigger: 'blur' }],
  winrm_port: [{ validator: validatePort, trigger: 'blur' }],
  remote_username: [
    {
      validator: (_rule, value, callback) => {
        if (formData.remote_password && !value.trim()) callback(new Error('填写密码时必须填写用户名'))
        else if (value && !value.trim()) callback(new Error('请输入有效的远程连接用户名'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  remote_password: [
    {
      validator: (_rule, value, callback) => {
        if (formData.remote_username && !value && (!isEdit.value || !props.device?.has_credential))
          callback(new Error('请同时填写远程连接密码'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
})

watch(
  () => props.visible,
  (val) => {
    cancelOsDetect()
    if (val) {
      if (props.device) {
        Object.assign(formData, {
          name: props.device.name,
          type: props.device.type,
          ip_address: props.device.ip_address,
          ssh_port: props.device.ssh_port,
          rdp_port: props.device.rdp_port,
          winrm_port: props.device.winrm_port ?? 5985,
          remote_username: props.device.credential_username || '',
          remote_password: '',
        })
      } else {
        Object.assign(formData, defaultFormData())
      }
      // 编辑态先沿用已保存的识别结果；新建态清空，等 IP 输入后自动探测。
      applyDetectResult(null)
      detectedFamily.value = osFamily(props.device?.os_system)
      detectedLabel.value = props.device?.os_system?.trim() || null
      detectUnidentified.value = false
      formRef.value?.clearValidate()
    }
  },
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
    const detectedOs = await detectOperatingSystem()
    const payload: DeviceSavePayload = {
      name: formData.name,
      type: formData.type,
      ip_address: formData.ip_address,
      os_system: detectedOs ?? props.device?.os_system ?? undefined,
      ssh_port: formData.ssh_port,
      rdp_port: formData.rdp_port,
      winrm_port: formData.winrm_port,
      remote_credential:
        formData.remote_username || formData.remote_password
          ? {
              username: formData.remote_username || undefined,
              password: formData.remote_password || undefined,
            }
          : null,
    }

    payload.position_u = props.presetU ?? props.device?.position_u ?? null
    payload.size_u = 2

    if (isEdit.value && props.device) {
      const saved = await deviceStore.updateDevice(props.device.id, payload)
      if (!saved) throw new Error('更新设备失败')
      ElMessage.success('设备更新成功')
    } else {
      const saved = await deviceStore.createDevice(props.rackId, payload)
      if (!saved) throw new Error('创建设备失败')
      ElMessage.success('设备创建成功')
    }

    emit('saved')
    handleClose()
  } catch (error: unknown) {
    // IP 查重等后端 422 的 detail 是真实原因(「IP 地址 x 已被设备 #y 使用」),
    // 统一替换成笼统文案会让人摸不着头脑。
    ElMessage.error(await extractErrorDetail(error, isEdit.value ? '更新设备失败' : '创建设备失败'))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.form-section {
  margin: var(--dcn-space-5) 0;
  padding: var(--dcn-space-4) var(--dcn-space-4) var(--dcn-space-1);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: var(--dcn-space-4);
}

.section-heading h3 {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
  font-weight: 600;
}

.section-heading p {
  margin: var(--dcn-space-1) 0 0;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.5;
}

.field-help {
  width: 100%;
  margin-top: var(--dcn-space-1);
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
  line-height: 1.5;
}

.detect-status {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-1);
  width: 100%;
  margin-top: var(--dcn-space-1);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
  line-height: 1.5;
}

.detect-status.is-muted {
  color: var(--dcn-text-placeholder);
}

.winrm-tools {
  width: 100%;
  margin-top: var(--dcn-space-2);
}

.winrm-alert {
  width: 100%;
  margin-top: var(--dcn-space-3);
}

:global(.device-form-dialog .el-dialog__body) {
  max-height: min(72vh, 760px);
  overflow-y: auto;
}

@media (max-width: 760px) {
  .form-section {
    padding-inline: var(--dcn-space-3);
  }
}
</style>
