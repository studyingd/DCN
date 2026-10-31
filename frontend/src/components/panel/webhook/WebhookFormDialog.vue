<template>
  <el-dialog
    v-model="formVisible"
    class="dcn-webhook-dialog"
    width="720px"
    top="6vh"
    :close-on-click-modal="false"
    @closed="resetForm"
  >
    <template #header>
      <div class="dialog-head">
        <strong>{{ editing ? '编辑告警通道' : '新建告警通道' }}</strong>
        <span>{{ editing ? `正在编辑「${editing.name}」` : '选择通道类型，填写接入信息与接收人' }}</span>
      </div>
    </template>

    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="webhook-form">
      <fieldset class="form-section">
        <legend>基本信息</legend>
        <div class="form-grid">
          <el-form-item label="通道名称" prop="name">
            <el-input v-model="form.name" maxlength="128" show-word-limit placeholder="例如：生产告警值班组" />
          </el-form-item>
          <el-form-item label="通道类型">
            <el-select v-model="form.provider" class="full-width">
              <el-option v-for="item in providers" :key="item.value" :label="item.label" :value="item.value">
                <span class="option-row">
                  <el-icon aria-hidden="true"><component :is="item.icon" /></el-icon>
                  <span class="option-text"
                    ><strong>{{ item.label }}</strong
                    ><small>{{ item.hint }}</small></span
                  >
                </span>
              </el-option>
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="启用状态">
          <el-switch v-model="form.enabled" />
          <span class="field-help">{{
            form.enabled ? '保存后立即开始推送已订阅的事件' : '停用后不会推送任何事件，配置会保留'
          }}</span>
        </el-form-item>
      </fieldset>

      <!-- 飞书自建应用固定走国内开放平台域名，不需要用户填写，整段隐藏 -->
      <fieldset v-if="!isFeishuApp" class="form-section">
        <legend>接入地址</legend>
        <el-form-item label="Webhook 地址" prop="url">
          <el-input v-model="form.url" spellcheck="false" :placeholder="WEBHOOK_PLACEHOLDER" />
          <span class="field-hint"
            >告警以 POST + JSON 发送；内网地址可用，回环 / 链路本地 / 云元数据地址会被拒绝（SSRF 防护）。</span
          >
        </el-form-item>
      </fieldset>

      <fieldset v-if="isFeishuApp" class="form-section">
        <legend>应用凭据</legend>
        <div class="form-grid">
          <el-form-item label="App ID">
            <el-input v-model="feishuForm.app_id" spellcheck="false" placeholder="cli_xxxxxxxxxxxxxxxx" />
          </el-form-item>
          <el-form-item label="App Secret">
            <el-input v-model="form.secret" type="password" show-password :placeholder="secretPlaceholder" />
          </el-form-item>
        </div>
        <p class="section-note">两者用于换取 tenant_access_token；Secret 加密存储、不回显，编辑时留空表示保持不变。</p>
        <el-collapse class="guide">
          <el-collapse-item name="guide">
            <template #title
              ><span class="guide-title"
                ><el-icon aria-hidden="true"><InfoFilled /></el-icon>如何在飞书开放平台创建自建应用？</span
              ></template
            >
            <ol class="guide-steps">
              <li>进入<em>飞书开放平台 → 创建企业自建应用</em>，在「应用能力」里开启<em>机器人</em>。</li>
              <li>申请权限 <code>im:message</code>（以应用身份发送单聊、群聊消息）。</li>
              <li>
                要从通讯录选人，再加
                <code>contact:contact.base:readonly</code
                >（获取通讯录基本信息）。飞书按<em>字段级权限</em>裁剪返回内容：姓名还要
                <code>contact:user.base:readonly</code>、部门名还要
                <code>contact:department.base:readonly</code>，缺了就只显示 ID。
              </li>
              <li>
                可选：<code>contact:user.email:readonly</code>
                只用于按邮箱搜索、副标题显示邮箱（投递不依赖邮箱，选人写入的是
                <code>open_id</code>）；授权范围里<em>单独勾选的成员</em>需要
                <code>contact:user.employee_id:readonly</code> 才读得到。权限改动都要重新<em>创建版本并发布</em>。
              </li>
              <li>
                让企业管理员在<em>飞书管理后台 → 工作台 → 应用管理 → 该应用 → 应用权限 → 通讯录设置</em
                >里设置「通讯录权限范围」：设为<em>全部员工</em>就能浏览整个组织架构；只勾选<em>指定部门 / 指定成员</em
                >也可以，此时选人弹窗只显示被授权的那部分。
              </li>
              <li>创建版本并发布，把凭证与基础信息里的 App ID / App Secret 填到上面。</li>
            </ol>
          </el-collapse-item>
        </el-collapse>
      </fieldset>

      <fieldset v-if="isFeishuApp" class="form-section">
        <legend>告警接收人</legend>
        <div class="receiver-toolbar">
          <el-button type="primary" plain :loading="directoryLoading" @click="openDirectory"
            ><el-icon aria-hidden="true"><OfficeBuilding /></el-icon>从通讯录选择</el-button
          >
          <span class="receiver-count" :class="{ over: feishuForm.receivers.length >= MAX_RECEIVERS }"
            >已选 {{ feishuForm.receivers.length }} / {{ MAX_RECEIVERS }}</span
          >
        </div>
        <div class="chip-editor">
          <span v-for="chip in receiverChips" :key="chip.id" class="receiver-chip editable">
            <el-icon aria-hidden="true"><UserFilled /></el-icon>
            <span class="chip-name" :title="chip.id">{{ chip.name }}</span>
            <em class="chip-type">{{ chip.typeLabel }}</em>
            <button
              type="button"
              class="chip-remove"
              :aria-label="`移除接收人 ${chip.name}`"
              @click="removeReceiver(chip.id)"
            >
              <el-icon aria-hidden="true"><Close /></el-icon>
            </button>
          </span>
          <span v-if="!feishuForm.receivers.length" class="chip-placeholder"
            >还没有接收人：从通讯录选人，或在下面手动添加</span
          >
        </div>
        <div class="manual-add">
          <el-input
            v-model="manualReceiver"
            clearable
            placeholder="手动添加：邮箱 / open_id(ou_) / union_id(on_) / 群 chat_id(oc_)，可一次粘贴多个"
            @keyup.enter="addManualReceiver"
          />
          <el-button :disabled="!manualReceiver.trim()" @click="addManualReceiver"
            ><el-icon aria-hidden="true"><Plus /></el-icon>添加</el-button
          >
        </div>
        <p class="section-note">
          无需选择 ID 类型：系统按填写内容自动识别邮箱 / open_id / union_id / 群 chat_id / user_id，并逐个投递。
        </p>
        <el-alert v-if="receiverError" type="error" :closable="false" show-icon class="inline-alert" role="alert">{{
          receiverError
        }}</el-alert>
      </fieldset>

      <fieldset v-if="!isFeishuApp" class="form-section">
        <legend>鉴权</legend>
        <el-form-item label="Secret（可选）">
          <el-input v-model="form.secret" type="password" show-password :placeholder="secretPlaceholder" />
          <span class="field-hint"
            >用于钉钉 / 飞书群机器人加签或自定义鉴权，随请求头
            <code>X-Webhook-Secret</code> 发送；加密存储且不回显。</span
          >
        </el-form-item>
      </fieldset>

      <fieldset class="form-section">
        <legend>订阅事件</legend>
        <el-checkbox-group v-model="form.events" class="event-grid">
          <el-checkbox v-for="item in eventOptions" :key="item.value" :value="item.value" class="event-option">
            <span class="event-text"
              ><strong>{{ item.label }}</strong
              ><small>{{ item.hint }}</small></span
            >
          </el-checkbox>
        </el-checkbox-group>
        <p v-if="!eventCount" class="section-note warn">未勾选任何事件时，这条通道不会收到推送。</p>
        <el-form-item label="级别过滤（可选）" class="severity-filter-item">
          <el-select
            v-model="severityFilter"
            multiple
            clearable
            collapse-tags
            placeholder="不过滤：全部级别都推送"
            style="width: 100%"
          >
            <el-option label="仅严重" value="critical" />
            <el-option label="仅警告" value="warning" />
            <el-option label="仅信息" value="info" />
          </el-select>
          <span class="field-hint">只推所选级别的告警——例如「严重」走值班群、「警告」走普通群；不选则不过滤。</span>
        </el-form-item>
      </fieldset>

      <el-collapse v-if="!isFeishuApp" class="form-section advanced">
        <el-collapse-item name="advanced">
          <template #title><span class="guide-title">高级：自定义请求头</span></template>
          <el-form-item label="请求头（JSON 对象，可选）">
            <el-input
              v-model="headersText"
              type="textarea"
              :rows="3"
              spellcheck="false"
              placeholder='例如：{"X-Environment":"production"}'
            />
            <span class="field-hint"
              >发送告警时与默认请求头合并；JSON 不合法则无法保存。飞书自建应用不使用该配置。</span
            >
          </el-form-item>
        </el-collapse-item>
      </el-collapse>
    </el-form>

    <template #footer>
      <span class="dialog-foot-note"
        ><el-icon aria-hidden="true"><Lock /></el-icon>Secret 使用 Fernet 加密存储，接口不会回显明文</span
      >
      <el-button @click="formVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submitForm">{{
        editing ? '保存修改' : '创建通道'
      }}</el-button>
    </template>
  </el-dialog>

  <FeishuDirectoryDialog ref="directoryRef" @apply="applyDirectoryResult" />
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { Close, InfoFilled, Lock, OfficeBuilding, Plus, UserFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { webhookAPI } from '@/api'
import type { Webhook, WebhookCreate, WebhookProviderConfig } from '@/types'
import FeishuDirectoryDialog from './FeishuDirectoryDialog.vue'
import { chipOf, eventOptions, FEISHU_BASE, MAX_RECEIVERS, providers, WEBHOOK_PLACEHOLDER } from './webhookMeta'
import type { ReceiverChip } from './webhookMeta'

const emit = defineEmits<{ (e: 'saved', hook: Webhook, editing: boolean): void }>()

const formVisible = ref(false)
const saving = ref(false)
const editing = ref<Webhook | null>(null)
const formRef = ref<FormInstance>()
const headersText = ref('')
const manualReceiver = ref('')
const receiverError = ref('')
const directoryRef = ref<InstanceType<typeof FeishuDirectoryDialog>>()
/** 「从通讯录选择」按钮的加载态:目录弹窗打开期间被遮罩盖住,这里只反映首次拉取 */
const directoryLoading = ref(false)

const form = reactive<WebhookCreate>({
  name: '',
  url: '',
  provider: 'generic',
  secret: '',
  events: ['alert.created', 'alert.resolved', 'alert.remediation'],
  headers: {},
  enabled: true,
})
const rules: FormRules = {
  name: [{ required: true, message: '请输入通道名称', trigger: 'blur' }],
  url: [
    { required: true, message: '请输入 http(s) 地址', trigger: 'blur' },
    { pattern: /^https?:\/\/.+/i, message: '地址必须以 http:// 或 https:// 开头', trigger: 'blur' },
  ],
}
/** 飞书自建应用的专属配置;接收人用数组管理，姓名映射只用于界面回显 */
const feishuForm = reactive({ app_id: '', receivers: [] as string[], receiver_names: {} as Record<string, string> })
/** 级别过滤(config.severities):空 = 不过滤(全部级别都推),旧行为不变 */
const severityFilter = ref<string[]>([])

const isFeishuApp = computed(() => form.provider === 'feishu_app')
const eventCount = computed(() => (form.events || []).length)
const secretPlaceholder = computed(() =>
  editing.value?.secret_set
    ? `已设置 ${editing.value.secret_preview}，留空保持不变`
    : isFeishuApp.value
      ? '飞书应用的 App Secret'
      : '用于签名或自定义鉴权',
)
/** 切到飞书自建应用时写入国内开放平台域名；切回其它通道要清掉，免得把飞书域名当 Webhook 提交 */
watch(
  () => form.provider,
  (value) => {
    if (value === 'feishu_app') {
      if (!form.url || form.url === WEBHOOK_PLACEHOLDER) form.url = FEISHU_BASE
    } else if (form.url === FEISHU_BASE) form.url = ''
  },
)
watch(
  () => feishuForm.receivers.length,
  (count) => {
    if (count) receiverError.value = ''
  },
)

const receiverChips = computed<ReceiverChip[]>(() =>
  feishuForm.receivers.map((id) => chipOf(id, feishuForm.receiver_names[id])),
)

function pushReceiver(id: string, name?: string) {
  const value = id.trim()
  if (!value) return false
  if (feishuForm.receivers.includes(value)) return false
  if (feishuForm.receivers.length >= MAX_RECEIVERS) {
    ElMessage.warning(`接收人上限 ${MAX_RECEIVERS} 个`)
    return false
  }
  feishuForm.receivers.push(value)
  if (name) feishuForm.receiver_names[value] = name
  receiverError.value = ''
  return true
}
function removeReceiver(id: string) {
  feishuForm.receivers = feishuForm.receivers.filter((item) => item !== id)
  delete feishuForm.receiver_names[id]
}
/** 手动添加支持一次粘贴多个，逗号 / 分号 / 空白分隔 */
function addManualReceiver() {
  const parts = manualReceiver.value
    .split(/[,;，；\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (!parts.length) return
  const added = parts.filter((part) => pushReceiver(part)).length
  manualReceiver.value = ''
  if (added) ElMessage.success(`已添加 ${added} 个接收人，ID 类型将自动识别`)
  else ElMessage.info('这些接收人已经在列表里了')
}

function openDirectory() {
  directoryRef.value?.open({
    webhookId: editing.value?.id ?? null,
    base: form.url,
    appId: feishuForm.app_id,
    appSecret: form.secret,
    secretSet: Boolean(editing.value?.secret_set),
    receivers: [...feishuForm.receivers],
    receiverNames: { ...feishuForm.receiver_names },
  })
}
/** 目录弹窗回写接收人:与原 applyDirectory 直写 feishuForm 等价 */
function applyDirectoryResult(receivers: string[], names: Record<string, string>) {
  feishuForm.receivers = receivers
  feishuForm.receiver_names = names
  receiverError.value = ''
}

function openForm(hook?: Webhook) {
  editing.value = hook || null
  Object.assign(
    form,
    hook
      ? {
          name: hook.name,
          url: hook.url,
          provider: hook.provider,
          secret: '',
          events: [...hook.events],
          headers: { ...hook.headers },
          enabled: hook.enabled,
        }
      : {
          name: '',
          url: '',
          provider: 'generic',
          secret: '',
          events: ['alert.created', 'alert.resolved', 'alert.remediation'],
          headers: {},
          enabled: true,
        },
  )
  const config = hook?.config || {}
  Object.assign(feishuForm, {
    app_id: config.app_id || '',
    receivers: [...(config.receivers || [])],
    receiver_names: { ...(config.receiver_names || {}) },
  })
  severityFilter.value = [...(config.severities || [])]
  headersText.value = hook ? JSON.stringify(hook.headers || {}, null, 2) : ''
  manualReceiver.value = ''
  receiverError.value = ''
  formVisible.value = true
}
function resetForm() {
  formRef.value?.resetFields()
  editing.value = null
  headersText.value = ''
  manualReceiver.value = ''
  receiverError.value = ''
  severityFilter.value = []
  Object.assign(feishuForm, { app_id: '', receivers: [], receiver_names: {} })
}
async function submitForm() {
  if (isFeishuApp.value && !form.url.trim()) form.url = FEISHU_BASE
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  let headers: Record<string, string> = {}
  if (headersText.value.trim()) {
    try {
      const parsed = JSON.parse(headersText.value)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error()
      headers = Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]))
    } catch {
      ElMessage.error('请求头必须是合法 JSON 对象')
      return
    }
  }
  let config: WebhookProviderConfig = {}
  if (isFeishuApp.value) {
    if (!feishuForm.app_id.trim()) {
      ElMessage.error('请填写飞书应用的 App ID')
      return
    }
    if (!feishuForm.receivers.length) {
      receiverError.value = '请至少选择一个接收人：从通讯录选人，或手动添加邮箱 / open_id / 群 chat_id'
      ElMessage.error(receiverError.value)
      return
    }
    if (!form.secret && !editing.value?.secret_set) {
      ElMessage.error('请填写飞书应用的 App Secret')
      return
    }
    config = {
      app_id: feishuForm.app_id.trim(),
      // 界面不提供 ID 类型与消息样式:类型由后端按接收人自动识别，消息统一用卡片
      receive_id_type: 'auto',
      receivers: [...feishuForm.receivers],
      receiver_names: { ...feishuForm.receiver_names },
      style: editing.value?.config?.style === 'text' ? 'text' : 'card',
    }
  }
  if (severityFilter.value.length) {
    // 级别过滤:配置了才写入,未配置不写键——后端对缺省键不过滤(旧行为)
    config.severities = [...severityFilter.value]
  }
  saving.value = true
  try {
    const payload = { ...form, headers, events: form.events || [], config }
    if (editing.value) {
      const res = await webhookAPI.update(editing.value.id, payload)
      emit('saved', res.data, true)
    } else {
      const res = await webhookAPI.create(payload)
      emit('saved', res.data, false)
    }
    ElMessage.success('Webhook 配置已保存')
    formVisible.value = false
  } catch (e) {
    ElMessage.error(errDetail(e, '保存 Webhook 失败'))
  } finally {
    saving.value = false
  }
}
function errDetail(e: unknown, fallback: string) {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
}

defineExpose({ open: openForm })
</script>

<style scoped>
.dialog-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.dialog-head strong {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
}
.dialog-head span {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  font-weight: 400;
}

/* ── 表单 ── */
.webhook-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.form-section {
  margin: 0 0 14px;
  padding: 14px 16px 4px;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-subtle);
}
.form-section legend {
  padding: 0 8px;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-base);
  font-weight: 600;
  letter-spacing: 0.02em;
}
.form-section :deep(.el-form-item) {
  margin-bottom: 14px;
}
.form-section :deep(.el-form-item__label) {
  padding-bottom: 4px;
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-base);
  line-height: 1.4;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 14px;
}
.full-width {
  width: 100%;
}
.field-hint {
  display: block;
  margin-top: 5px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.6;
}
.field-hint code,
.section-note code {
  padding: 1px 5px;
  border-radius: var(--dcn-radius-xs);
  background: var(--dcn-primary-bg);
  color: var(--dcn-primary-light);
  font: var(--dcn-text-xs) var(--dcn-font-mono);
}
.field-help {
  margin-left: 10px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.section-note {
  margin: 0 0 12px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.6;
}
.section-note.warn {
  color: var(--dcn-warning-text);
}
.inline-alert {
  margin-bottom: 12px;
  padding: 8px 12px;
}
.guide {
  margin-bottom: 14px;
  border: 0;
}
.guide :deep(.el-collapse-item__header) {
  height: 32px;
  border: 0;
  background: none;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 32px;
}
.guide :deep(.el-collapse-item__wrap) {
  border: 0;
  background: none;
}
.guide :deep(.el-collapse-item__content) {
  padding-bottom: 10px;
}
.guide-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.guide-steps {
  margin: 0;
  padding-left: 18px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.8;
}
.guide-steps em {
  color: var(--dcn-text-regular);
  font-style: normal;
}
.receiver-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.receiver-count {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.receiver-count.over {
  color: var(--dcn-warning-text);
}
/* 芯片换行而不是裁切;接收人多时限定高度内部滚动，避免弹窗被撑得很长 */
.chip-editor {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  min-height: 44px;
  max-height: 168px;
  margin-bottom: 10px;
  padding: 8px 10px;
  overflow: auto;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-input-bg);
}
.manual-add {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.manual-add :deep(.el-input) {
  flex: 1;
}
.severity-filter-item {
  margin-top: 12px;
}
.event-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 2px 14px;
  margin-bottom: 14px;
}
.event-option {
  display: flex;
  align-items: flex-start;
  height: auto;
  margin-right: 0;
  padding: 6px 8px;
  border-radius: var(--dcn-radius-sm);
  /* grid 子项默认 min-width:auto，而中文串没有空格、min-content 就是整串宽度，
     长提示会把列撑到超出容器（巡检报告那条就是这么溢出的）。
     置 0 后才允许收缩、让文字在列内换行。 */
  min-width: 0;
}
.event-option:hover {
  background: var(--dcn-primary-bg);
}
.event-option :deep(.el-checkbox__input) {
  margin-top: 3px;
}
.event-option :deep(.el-checkbox__label) {
  padding-left: 8px;
  line-height: 1.4;
}
.event-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.event-text strong {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-base);
  font-weight: 500;
}
.event-text small {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.advanced {
  margin-bottom: 6px;
  border-top: 1px solid var(--dcn-border-light);
}
.advanced :deep(.el-collapse-item__header) {
  height: 38px;
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-base);
}
.option-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.option-text {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}
.option-text strong {
  font-size: var(--dcn-text-base);
  font-weight: 500;
}
.option-text small {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}

/* ── 接收人芯片(编辑器) ── */
.receiver-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  padding: 3px 9px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-pill);
  background: var(--dcn-bg-muted);
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
  line-height: 1.5;
}
.receiver-chip .el-icon {
  color: var(--dcn-primary-light);
  font-size: 12px;
}
.receiver-chip.editable {
  padding-right: 4px;
}
.chip-name {
  overflow: hidden;
  max-width: 180px;
  color: var(--dcn-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-type {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
  font-style: normal;
}
.chip-remove {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--dcn-radius-full);
  background: none;
  color: var(--dcn-text-secondary);
  cursor: pointer;
  transition:
    background var(--dcn-transition-fast),
    color var(--dcn-transition-fast);
}
.chip-remove:hover {
  background: var(--dcn-danger-bg);
  color: var(--dcn-danger-text);
}
.chip-placeholder {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}

/* ── 可访问性:键盘焦点必须有可见指示 ── */
.chip-remove:focus-visible {
  outline: 2px solid var(--dcn-focus-ring);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .chip-remove {
    transition: none;
  }
}

@media (max-width: 720px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
  .manual-add {
    flex-direction: column;
  }
}
</style>

<style>
/* 弹窗会被 teleport 到 body，scoped 的 :deep() 覆盖不到外壳，这里用独立类名做非作用域样式 */
.dcn-webhook-dialog .el-dialog__body {
  padding-top: 8px;
}
.dcn-webhook-dialog .el-dialog__footer {
  display: flex;
  align-items: center;
  gap: 10px;
}
.dcn-webhook-dialog .dialog-foot-note {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-right: auto;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
</style>
