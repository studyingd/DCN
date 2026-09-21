<template>
  <section class="webhook-settings">
    <header class="panel-head">
      <div class="head-text">
        <h3>Webhook 告警通道</h3>
        <p>把系统事件推送到企业微信、钉钉、飞书（工作流 / 群机器人 / 自建应用私聊）或自定义 HTTP 接收端。</p>
      </div>
      <div class="head-actions">
        <el-button :loading="loading" @click="load"
          ><el-icon aria-hidden="true"><Refresh /></el-icon>刷新</el-button
        >
        <el-button type="primary" @click="openForm()"
          ><el-icon aria-hidden="true"><Plus /></el-icon>新建通道</el-button
        >
      </div>
    </header>

    <div v-if="webhooks.length" class="summary-strip" role="group" aria-label="通道概览">
      <div class="summary-item">
        <span>通道总数</span><strong>{{ webhooks.length }}</strong>
      </div>
      <div class="summary-item">
        <span>已启用</span><strong class="ok">{{ enabledCount }}</strong>
      </div>
      <div class="summary-item">
        <span>测试失败</span><strong :class="failedCount ? 'bad' : ''">{{ failedCount }}</strong>
      </div>
      <div class="summary-item">
        <span>飞书私聊接收人</span><strong>{{ feishuReceiverCount }}</strong>
      </div>
    </div>

    <div v-else-if="!loading" class="empty-state">
      <span class="empty-icon" aria-hidden="true"
        ><el-icon><Bell /></el-icon
      ></span>
      <h4>还没有告警通道</h4>
      <p>配置一条通道后，告警触发、自动处置进度与 AI 归因都会实时推送到值班人。</p>
      <ol class="empty-steps">
        <li v-for="(step, index) in onboardingSteps" :key="step">
          <span class="step-index" aria-hidden="true">{{ index + 1 }}</span
          ><span>{{ step }}</span>
        </li>
      </ol>
      <el-button type="primary" @click="openForm()"
        ><el-icon aria-hidden="true"><Plus /></el-icon>新建第一条通道</el-button
      >
    </div>

    <ul v-loading="loading" class="webhook-grid">
      <li v-for="hook in webhooks" :key="hook.id" class="webhook-card" :class="{ 'is-off': !hook.enabled }">
        <div class="card-head">
          <span class="provider-icon" :class="`provider-${hook.provider}`" aria-hidden="true"
            ><el-icon><component :is="providerIcon(hook.provider)" /></el-icon
          ></span>
          <div class="card-title">
            <strong :title="hook.name">{{ hook.name }}</strong>
            <span>{{ providerLabel(hook.provider) }}</span>
          </div>
          <el-tag v-if="!hook.enabled" size="small" type="info" effect="plain" round>已停用</el-tag>
          <el-switch
            :model-value="hook.enabled"
            :aria-label="`启用或停用通道 ${hook.name}`"
            @change="toggleEnabled(hook, $event as boolean)"
          />
        </div>

        <div class="card-target">
          <template v-if="hook.provider === 'feishu_app'">
            <span class="target-label">接收人</span>
            <div class="chip-row">
              <span v-for="chip in chipsOf(hook).slice(0, 4)" :key="chip.id" class="receiver-chip" :title="chip.id"
                ><el-icon aria-hidden="true"><UserFilled /></el-icon>{{ chip.name }}</span
              >
              <el-tooltip
                v-if="chipsOf(hook).length > 4"
                :content="
                  chipsOf(hook)
                    .slice(4)
                    .map((chip) => chip.name)
                    .join('、')
                "
                placement="top"
              >
                <span class="receiver-chip more">+{{ chipsOf(hook).length - 4 }}</span>
              </el-tooltip>
              <span v-if="!chipsOf(hook).length" class="muted">未配置接收人</span>
            </div>
          </template>
          <p v-else class="endpoint" :title="hook.url">{{ hook.url }}</p>
        </div>

        <div class="event-list">
          <el-tag v-for="event in hook.events.slice(0, 3)" :key="event" size="small" effect="plain">{{
            eventLabel(event)
          }}</el-tag>
          <el-tooltip
            v-if="hook.events.length > 3"
            :content="hook.events.slice(3).map(eventLabel).join('、')"
            placement="top"
          >
            <el-tag size="small" effect="plain" type="info">+{{ hook.events.length - 3 }}</el-tag>
          </el-tooltip>
          <span v-if="!hook.events.length" class="muted">未订阅事件</span>
        </div>

        <dl class="card-meta">
          <div class="meta-item">
            <dt>Secret</dt>
            <dd>{{ hook.secret_set ? hook.secret_preview : '未设置' }}</dd>
          </div>
          <div class="meta-item">
            <dt>最近测试</dt>
            <dd :class="hook.last_test_status === 'success' ? 'ok' : hook.last_test_status === 'failed' ? 'bad' : ''">
              <el-icon v-if="hook.last_test_status === 'success'" aria-hidden="true"><CircleCheck /></el-icon>
              <el-icon v-else-if="hook.last_test_status === 'failed'" aria-hidden="true"><CircleClose /></el-icon>
              {{ testLabel(hook) }}
            </dd>
          </div>
        </dl>

        <div class="card-actions">
          <el-button link type="primary" :loading="testingId === hook.id" @click="testWebhook(hook)"
            ><el-icon v-if="testingId !== hook.id" aria-hidden="true"><Promotion /></el-icon>测试发送</el-button
          >
          <el-button link @click="openForm(hook)"
            ><el-icon aria-hidden="true"><Edit /></el-icon>编辑</el-button
          >
          <el-button link type="danger" @click="removeWebhook(hook)"
            ><el-icon aria-hidden="true"><Delete /></el-icon>删除</el-button
          >
        </div>
      </li>
    </ul>

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
          <p class="section-note">
            两者用于换取 tenant_access_token；Secret 加密存储、不回显，编辑时留空表示保持不变。
          </p>
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
                  >里设置「通讯录权限范围」：设为<em>全部员工</em>就能浏览整个组织架构；只勾选<em
                    >指定部门 / 指定成员</em
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
            <el-button type="primary" plain :loading="directory.loading" @click="openDirectory"
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

    <el-dialog
      v-model="directory.visible"
      class="dcn-directory-dialog"
      width="880px"
      top="6vh"
      append-to-body
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="dialog-head">
          <strong>从飞书通讯录选择接收人</strong>
          <span>按部门逐层下钻或直接搜索；选中后写入 open_id，界面上显示姓名</span>
        </div>
      </template>

      <p v-if="directory.scopeLimited" class="scope-note">
        该应用只被授权了部分通讯录，下面显示的是「通讯录权限范围」内的部门与成员。需要看到更多，请让管理员在
        <em>飞书管理后台 → 工作台 → 应用管理 → 该应用 → 应用权限 → 通讯录设置</em> 里扩大范围。
      </p>
      <el-alert v-if="scopeHint" type="warning" :closable="false" show-icon class="inline-alert" role="status">
        {{ scopeHint }}
      </el-alert>
      <div class="directory">
        <div class="directory-side">
          <div class="directory-crumb">
            <button
              v-if="directory.trail.length"
              type="button"
              class="crumb-back"
              @click="gotoTrail(directory.trail.length - 2)"
            >
              <el-icon aria-hidden="true"><Back /></el-icon>返回上级
            </button>
            <nav class="crumb-trail" aria-label="部门层级">
              <button type="button" class="crumb" :class="{ current: !directory.trail.length }" @click="gotoTrail(-1)">
                通讯录
              </button>
              <template v-for="(node, index) in directory.trail" :key="node.id">
                <span class="crumb-sep" aria-hidden="true">/</span>
                <button
                  type="button"
                  class="crumb"
                  :class="{ current: index === directory.trail.length - 1 }"
                  @click="gotoTrail(index)"
                >
                  {{ node.name }}
                </button>
              </template>
            </nav>
          </div>
          <ul v-loading="directory.loading" class="department-list">
            <li v-for="dep in directory.departments" :key="dep.open_department_id">
              <button type="button" class="department" @click="enterDepartment(dep)">
                <el-icon aria-hidden="true"><OfficeBuilding /></el-icon>
                <span class="department-name">{{ dep.name }}</span>
                <el-icon class="department-arrow" aria-hidden="true"><ArrowRight /></el-icon>
              </button>
            </li>
            <li v-if="!directory.departments.length && !directory.loading" class="directory-empty">该层级没有子部门</li>
          </ul>
        </div>

        <div class="directory-main">
          <div class="directory-search">
            <el-input
              v-model="directory.keyword"
              clearable
              :prefix-icon="Search"
              placeholder="搜索当前部门成员的姓名、邮箱或 ID"
              aria-label="搜索成员"
            />
            <span class="result-count">{{ filteredUsers.length }} 人</span>
          </div>
          <el-alert v-if="directory.error" type="error" :closable="false" show-icon class="inline-alert" role="alert">{{
            directory.error
          }}</el-alert>
          <ul v-loading="directory.loading" class="user-list">
            <li v-for="user in filteredUsers" :key="user.open_id">
              <el-checkbox
                class="user"
                :model-value="directory.selected.includes(user.open_id)"
                @change="toggleUser(user, $event as boolean)"
              >
                <span class="user-text"
                  ><strong>{{ user.name }}</strong
                  ><small>{{ userSubtitle(user) }}</small></span
                >
              </el-checkbox>
            </li>
            <li v-if="!filteredUsers.length && !directory.loading && !directory.error" class="directory-empty">
              {{ usersEmptyText }}
            </li>
          </ul>
          <el-button v-if="directory.hasMore" link type="primary" :loading="directory.loading" @click="loadMoreUsers"
            >加载更多成员</el-button
          >
        </div>
      </div>

      <template #footer>
        <div class="selected-tray">
          <span class="tray-label">已选 {{ directory.selected.length }} 人</span>
          <span v-for="chip in selectedChips" :key="chip.id" class="receiver-chip editable">
            {{ chip.name }}
            <button type="button" class="chip-remove" :aria-label="`取消选择 ${chip.name}`" @click="unselect(chip.id)">
              <el-icon aria-hidden="true"><Close /></el-icon>
            </button>
          </span>
          <span v-if="!directory.selected.length" class="tray-hint">取消勾选已在列表中的人，写入时会一并移除</span>
        </div>
        <el-button @click="directory.visible = false">取消</el-button>
        <el-button type="primary" @click="applyDirectory">写入接收人</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import type { Component } from 'vue'
import {
  ArrowRight,
  Back,
  Bell,
  ChatDotRound,
  ChatDotSquare,
  ChatLineRound,
  CircleCheck,
  CircleClose,
  Close,
  Delete,
  Edit,
  InfoFilled,
  Link,
  Lock,
  Message,
  OfficeBuilding,
  Plus,
  Promotion,
  Refresh,
  Search,
  Share,
  UserFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { webhookAPI } from '@/api'
import type {
  FeishuDepartment,
  FeishuDirectoryRequest,
  FeishuUser,
  Webhook,
  WebhookCreate,
  WebhookProviderConfig,
} from '@/types'
import { formatDateTime } from '@/utils/datetime'

/** 飞书自建应用固定使用国内开放平台域名，界面上不再暴露该配置项 */
const FEISHU_BASE = 'https://open.feishu.cn'
const WEBHOOK_PLACEHOLDER = 'https://example.com/webhook'
/** 与后端 feishu._MAX_RECEIVERS 一致 */
const MAX_RECEIVERS = 50

const webhooks = ref<Webhook[]>([])
const loading = ref(false)
const saving = ref(false)
const testingId = ref<number | null>(null)
const formVisible = ref(false)
const editing = ref<Webhook | null>(null)
const formRef = ref<FormInstance>()
const headersText = ref('')
const manualReceiver = ref('')
const receiverError = ref('')

interface ProviderOption {
  value: string
  label: string
  hint: string
  icon: Component
}
const providers: ProviderOption[] = [
  { value: 'feishu_app', label: '飞书自建应用', hint: '直接私聊指定的人，可从通讯录选人', icon: Message },
  { value: 'feishu_bot', label: '飞书群机器人', hint: '发到机器人所在的群，支持加签', icon: ChatDotSquare },
  { value: 'feishu', label: '飞书工作流', hint: '多维表格 / 审批流程的 Webhook', icon: Share },
  { value: 'wecom', label: '企业微信', hint: '群机器人 Webhook，POST JSON', icon: ChatDotRound },
  { value: 'dingtalk', label: '钉钉', hint: '群机器人 Webhook，支持加签', icon: ChatLineRound },
  { value: 'generic', label: '通用 HTTP', hint: '任意接收端，POST 完整告警 JSON', icon: Link },
]

const eventOptions = [
  { value: 'alert.created', label: '告警触发', hint: '指标越限或主机 / 容器离线' },
  { value: 'alert.resolved', label: '告警恢复', hint: '指标回落或对象恢复在线' },
  { value: 'alert.remediation', label: '自动处置进度', hint: '虚拟机 / 容器自动拉起的每个阶段' },
  {
    value: 'alert.analysis',
    label: 'AI 归因结论',
    hint: '兜底：仅看门狗超时先发告警卡时，归因完成后补发',
  },
  {
    value: 'automation.notify',
    label: '自动化通知',
    hint: '自动化任务结论，巡检附 PDF',
  },
]
const eventMap = computed(() => Object.fromEntries(eventOptions.map((item) => [item.value, item.label])))
const onboardingSteps = [
  '选择通道类型：飞书私聊 / 群机器人，或企业微信、钉钉、自定义 HTTP',
  '填写接入信息：飞书自建应用填 App ID 与 App Secret，其它通道填 Webhook 地址',
  '指定接收人并勾选要推送的事件',
  '点「测试发送」，确认能收到样例告警卡片',
]

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

const ID_TYPE_LABEL: Record<string, string> = {
  email: '邮箱',
  open_id: 'open_id',
  union_id: 'union_id',
  chat_id: '群',
  user_id: 'user_id',
}
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
/** 与后端 feishu.infer_receive_id_type 一致:界面不再让用户挑 ID 类型 */
function inferIdType(value: string) {
  const text = value.trim()
  if (EMAIL_PATTERN.test(text)) return 'email'
  if (text.startsWith('ou_')) return 'open_id'
  if (text.startsWith('on_')) return 'union_id'
  if (text.startsWith('oc_')) return 'chat_id'
  return 'user_id'
}

interface ReceiverChip {
  id: string
  name: string
  typeLabel: string
}
function chipOf(id: string, name?: string): ReceiverChip {
  return { id, name: name || id, typeLabel: ID_TYPE_LABEL[inferIdType(id)] || id }
}
const receiverChips = computed<ReceiverChip[]>(() =>
  feishuForm.receivers.map((id) => chipOf(id, feishuForm.receiver_names[id])),
)
const cardChips = computed(() => {
  const map = new Map<number, ReceiverChip[]>()
  for (const hook of webhooks.value) {
    const names = hook.config?.receiver_names || {}
    map.set(
      hook.id,
      (hook.config?.receivers || []).map((id) => chipOf(id, names[id])),
    )
  }
  return map
})
function chipsOf(hook: Webhook) {
  return cardChips.value.get(hook.id) || []
}

const enabledCount = computed(() => webhooks.value.filter((hook) => hook.enabled).length)
const failedCount = computed(() => webhooks.value.filter((hook) => hook.last_test_status === 'failed').length)
const feishuReceiverCount = computed(() =>
  webhooks.value.reduce(
    (sum, hook) => sum + (hook.provider === 'feishu_app' ? (hook.config?.receivers || []).length : 0),
    0,
  ),
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

/** 飞书通讯录选人弹窗:左侧部门逐层下钻，右侧成员多选，底部已选托盘 */
const directory = reactive({
  visible: false,
  loading: false,
  error: '',
  keyword: '',
  trail: [] as { id: string; name: string }[],
  departments: [] as FeishuDepartment[],
  users: [] as FeishuUser[],
  selected: [] as string[],
  /** open_id → 接收人列表里已有的那个值(可能是邮箱)，取消勾选时据此精确移除 */
  matched: {} as Record<string, string>,
  /** open_id → 姓名，写回列表后用于回显 */
  names: {} as Record<string, string>,
  pageToken: '',
  hasMore: false,
  /** 后端按「通讯录权限范围」兜底返回时置真，用于提示只看到部分组织架构 */
  scopeLimited: false,
  /** 飞书按字段级权限裁掉了名称:记下缺的是成员姓名还是部门名，提示去补哪个权限 */
  missingNames: [] as ('user' | 'dept')[],
})
const filteredUsers = computed(() => {
  const keyword = directory.keyword.trim().toLowerCase()
  if (!keyword) return directory.users
  return directory.users.filter((user) =>
    [user.name, user.email, user.enterprise_email, user.open_id, user.user_id].some((value) =>
      (value || '').toLowerCase().includes(keyword),
    ),
  )
})
const selectedChips = computed(() => directory.selected.map((id) => ({ id, name: directory.names[id] || id })))
/** 名称读不到时直接说清缺哪个权限，别让运维对着一堆 ID 猜 */
const scopeHint = computed(() => {
  const missing = [
    ...(directory.missingNames.includes('user') ? ['成员姓名要 contact:user.base:readonly'] : []),
    ...(directory.missingNames.includes('dept') ? ['部门名要 contact:department.base:readonly'] : []),
  ]
  if (!missing.length) return ''
  return `飞书按字段级权限裁剪了返回内容：${missing.join('；')}。缺的字段只能显示 ID，请在飞书开放平台为应用补上权限、创建版本并发布后重新打开本弹窗。（邮箱 contact:user.email:readonly 可选，只影响按邮箱搜索与副标题显示。）`
})
/** 成员空态文案:部分授权时要说清楚为什么看不到人、缺哪个权限 */
const usersEmptyText = computed(() => {
  if (directory.keyword) return `当前部门没有匹配「${directory.keyword}」的成员，换个关键词或到其它部门看看`
  if (directory.scopeLimited)
    return '授权范围里没有单独勾选的成员。若管理员确实勾选了人却读不到，需要在飞书开放平台为应用补 contact:user.employee_id:readonly 权限并重新发布版本'
  return '该部门下没有成员，或应用未被授权查看这个部门'
})
function userSubtitle(user: FeishuUser) {
  return user.enterprise_email || user.email || user.open_id
}
function toggleUser(user: FeishuUser, checked: boolean) {
  directory.names[user.open_id] = user.name
  directory.selected = checked
    ? [...new Set([...directory.selected, user.open_id])]
    : directory.selected.filter((id) => id !== user.open_id)
}
function unselect(openId: string) {
  directory.selected = directory.selected.filter((id) => id !== openId)
}

function directoryCredentials(): FeishuDirectoryRequest | null {
  const appId = feishuForm.app_id.trim()
  if (!appId) {
    ElMessage.warning('请先填写 App ID')
    return null
  }
  // 编辑已保存的通道时 Secret 可以留空，后端回落到库里加密存的那份
  if (!form.secret && !editing.value?.secret_set) {
    ElMessage.warning('请先填写 App Secret 再读取通讯录')
    return null
  }
  return { webhook_id: editing.value?.id ?? null, base: form.url, app_id: appId, app_secret: form.secret || undefined }
}
function currentDepartmentId() {
  return directory.trail.length ? directory.trail[directory.trail.length - 1].id : '0'
}
/** 记下姓名，并把"已经在接收人列表里的人"回显成勾选状态 */
function absorbUsers(users: FeishuUser[]) {
  for (const user of users) {
    directory.names[user.open_id] = user.name
    const identities = [user.open_id, user.user_id, user.union_id, user.email, user.enterprise_email].filter(Boolean)
    const hit = feishuForm.receivers.find((item) => identities.includes(item))
    if (!hit) continue
    directory.matched[user.open_id] = hit
    if (!directory.selected.includes(user.open_id)) directory.selected.push(user.open_id)
  }
}
async function fetchDirectory(departmentId: string, append = false) {
  const creds = directoryCredentials()
  if (!creds) {
    directory.visible = false
    return
  }
  directory.loading = true
  try {
    const [departments, users] = await Promise.all([
      webhookAPI.feishuDepartments({ ...creds, department_id: departmentId }),
      webhookAPI.feishuUsers({ ...creds, department_id: departmentId, page_token: append ? directory.pageToken : '' }),
    ])
    directory.users = append ? [...directory.users, ...users.data.items] : users.data.items
    directory.pageToken = users.data.page_token
    directory.hasMore = users.data.has_more
    if (!append) {
      directory.departments = departments.data.items
      directory.scopeLimited = Boolean(departments.data.scope_limited || users.data.scope_limited)
      const missing: ('user' | 'dept')[] = []
      if (users.data.name_scope_missing) missing.push('user')
      if (departments.data.name_scope_missing) missing.push('dept')
      directory.missingNames = missing
      directory.error = ''
    }
    absorbUsers(users.data.items)
  } catch (e) {
    directory.error = errDetail(e, '读取飞书通讯录失败')
    if (!append) {
      directory.departments = []
      directory.users = []
      directory.hasMore = false
    }
  } finally {
    directory.loading = false
  }
}
async function openDirectory() {
  if (!directoryCredentials()) return
  Object.assign(directory, {
    visible: true,
    loading: false,
    error: '',
    keyword: '',
    trail: [],
    departments: [],
    users: [],
    selected: [],
    matched: {},
    names: {},
    pageToken: '',
    hasMore: false,
    scopeLimited: false,
    missingNames: [],
  })
  await fetchDirectory('0')
}
async function enterDepartment(dep: FeishuDepartment) {
  directory.trail.push({ id: dep.open_department_id, name: dep.name })
  await fetchDirectory(dep.open_department_id)
}
async function gotoTrail(index: number) {
  directory.trail = index < 0 ? [] : directory.trail.slice(0, index + 1)
  await fetchDirectory(currentDepartmentId())
}
async function loadMoreUsers() {
  await fetchDirectory(currentDepartmentId(), true)
}
function applyDirectory() {
  const unchecked = Object.entries(directory.matched)
    .filter(([openId]) => !directory.selected.includes(openId))
    .map(([, stored]) => stored)
  const kept = feishuForm.receivers.filter((item) => !unchecked.includes(item))
  const merged = [...kept]
  const names: Record<string, string> = {}
  for (const [id, name] of Object.entries(feishuForm.receiver_names)) if (kept.includes(id)) names[id] = name
  for (const openId of directory.selected) {
    // 同一个人原本用邮箱存的，就继续用邮箱，避免重复投递
    const value = directory.matched[openId] || openId
    if (!merged.includes(value)) merged.push(value)
    const name = directory.names[openId]
    if (name) names[value] = name
  }
  feishuForm.receivers = merged.slice(0, MAX_RECEIVERS)
  feishuForm.receiver_names = Object.fromEntries(
    Object.entries(names).filter(([id]) => feishuForm.receivers.includes(id)),
  )
  directory.visible = false
  receiverError.value = ''
  ElMessage.success(
    unchecked.length
      ? `已写入 ${directory.selected.length} 位接收人，移除 ${unchecked.length} 位`
      : `已写入 ${directory.selected.length} 位接收人`,
  )
}

function providerIcon(value: string) {
  return providers.find((item) => item.value === value)?.icon || Bell
}
function providerLabel(value: string) {
  return providers.find((item) => item.value === value)?.label || value
}
function eventLabel(value: string) {
  return eventMap.value[value] || value
}
function testLabel(hook: Webhook) {
  return hook.last_test_at
    ? `${hook.last_test_status === 'success' ? '测试成功' : '测试失败'} · ${formatTime(hook.last_test_at)}`
    : '尚未测试'
}
function formatTime(value: string) {
  return formatDateTime(value)
}
function errDetail(e: unknown, fallback: string) {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
}

async function load() {
  loading.value = true
  try {
    webhooks.value = (await webhookAPI.list()).data
  } catch {
    ElMessage.error('加载 Webhook 失败')
  } finally {
    loading.value = false
  }
}
async function toggleEnabled(hook: Webhook, enabled: boolean) {
  try {
    const res = await webhookAPI.update(hook.id, { enabled })
    Object.assign(hook, res.data)
    ElMessage.success(enabled ? 'Webhook 已启用' : 'Webhook 已停用')
  } catch {
    ElMessage.error('状态更新失败')
  }
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
      const idx = webhooks.value.findIndex((item) => item.id === editing.value!.id)
      if (idx >= 0) webhooks.value[idx] = res.data
    } else {
      webhooks.value.unshift((await webhookAPI.create(payload)).data)
    }
    ElMessage.success('Webhook 配置已保存')
    formVisible.value = false
  } catch (e) {
    ElMessage.error(errDetail(e, '保存 Webhook 失败'))
  } finally {
    saving.value = false
  }
}
async function removeWebhook(hook: Webhook) {
  try {
    await ElMessageBox.confirm(`确定删除“${hook.name}”吗？`, '删除确认', { type: 'warning' })
    await webhookAPI.remove(hook.id)
    webhooks.value = webhooks.value.filter((item) => item.id !== hook.id)
    ElMessage.success('Webhook 已删除')
  } catch {
    /* cancelled */
  }
}
async function testWebhook(hook: Webhook) {
  testingId.value = hook.id
  try {
    const res = await webhookAPI.test(hook.id)
    Object.assign(hook, {
      last_test_status: res.data.ok ? 'success' : 'failed',
      last_test_at: new Date().toISOString(),
    })
    if (res.data.ok) ElMessage.success(`${res.data.message}（${res.data.duration_ms} ms）`)
    else ElMessage.error(res.data.message)
  } catch {
    ElMessage.error('Webhook 测试失败')
  } finally {
    testingId.value = null
  }
}
onMounted(load)
</script>

<style scoped>
.webhook-settings {
  max-width: 1080px;
}

/* ── 页头与概览 ── */
.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.head-text h3 {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-xl);
}
.head-text p {
  margin: 6px 0 0;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-base);
  line-height: 1.6;
}
.head-actions {
  display: flex;
  flex-shrink: 0;
  gap: 8px;
}
.summary-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1px;
  margin-bottom: 16px;
  overflow: hidden;
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-border-light);
}
.summary-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 14px;
  background: var(--dcn-bg-card);
}
.summary-item span {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.summary-item strong {
  color: var(--dcn-text-primary);
  font: 600 var(--dcn-text-lg) var(--dcn-font-mono);
}

/* ── 空状态 ── */
.empty-state {
  padding: 28px 24px;
  border: 1px dashed var(--dcn-border-strong);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-card-soft);
  text-align: center;
}
.empty-icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin: 0 auto 12px;
  border-radius: var(--dcn-radius-2xl);
  color: var(--dcn-primary-light);
  background: var(--dcn-primary-bg);
  font-size: 20px;
}
.empty-state h4 {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
}
.empty-state p {
  margin: 6px auto 16px;
  max-width: 520px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-base);
  line-height: 1.6;
}
.empty-steps {
  display: grid;
  gap: 8px;
  margin: 0 auto 18px;
  max-width: 520px;
  padding: 0;
  list-style: none;
  text-align: left;
}
.empty-steps li {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-base);
  line-height: 1.6;
}
.step-index {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  border-radius: var(--dcn-radius-full);
  background: var(--dcn-primary-bg);
  color: var(--dcn-primary-light);
  font: 600 var(--dcn-text-sm) var(--dcn-font-mono);
}

/* ── 通道卡片 ── */
.webhook-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
  min-height: 60px;
}
.webhook-card {
  display: flex;
  flex-direction: column;
  padding: 14px 16px 10px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-card);
  box-shadow: var(--dcn-shadow-sm);
  transition:
    border-color var(--dcn-transition-fast),
    box-shadow var(--dcn-transition-fast),
    transform var(--dcn-transition-fast);
}
.webhook-card:hover {
  border-color: var(--dcn-border-strong);
  box-shadow: var(--dcn-shadow-md);
  transform: translateY(-1px);
}
.webhook-card.is-off {
  background: var(--dcn-bg-subtle);
}
.webhook-card.is-off .card-title strong {
  color: var(--dcn-text-secondary);
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.provider-icon {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: var(--dcn-radius-xl);
  color: var(--dcn-primary-light);
  background: var(--dcn-primary-bg);
  font-size: 16px;
}
.provider-generic {
  color: #a5b4fc;
  background: rgba(99, 102, 241, 0.14);
}
.provider-wecom {
  color: #34d399;
  background: rgba(16, 185, 129, 0.14);
}
.provider-dingtalk {
  color: #60a5fa;
  background: rgba(59, 130, 246, 0.14);
}
.provider-feishu {
  color: #fbbf24;
  background: rgba(245, 158, 11, 0.14);
}
.provider-feishu_bot {
  color: #2dd4bf;
  background: rgba(20, 184, 166, 0.14);
}
.provider-feishu_app {
  color: #38bdf8;
  background: rgba(14, 165, 233, 0.14);
}
.card-title {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.card-title strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-title span {
  margin-top: 3px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.card-target {
  margin: 12px 0 10px;
}
.target-label {
  display: block;
  margin-bottom: 6px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.endpoint {
  margin: 0;
  overflow: hidden;
  color: var(--dcn-text-secondary);
  font: var(--dcn-text-sm) var(--dcn-font-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.event-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-height: 24px;
}
.card-meta {
  display: flex;
  gap: 8px;
  margin: 12px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--dcn-border-light);
}
.meta-item {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.meta-item dt {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.meta-item dd {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  overflow: hidden;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 2px;
  margin: 6px -6px 0;
  padding-top: 6px;
  border-top: 1px solid var(--dcn-border-light);
}
.muted {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}
.ok {
  color: var(--dcn-success-text);
}
.bad {
  color: var(--dcn-danger-text);
}

/* ── 接收人芯片 ── */
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
.receiver-chip.more {
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
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
.scope-note {
  margin: 0 0 12px;
  padding: 8px 10px;
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-primary-bg);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.6;
}
.scope-note em {
  color: var(--dcn-text-regular);
  font-style: normal;
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

/* ── 通讯录弹窗 ── */
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
.directory {
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  gap: 14px;
  min-height: 420px;
}
.directory-side {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding-right: 12px;
  border-right: 1px solid var(--dcn-border-light);
}
.directory-crumb {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}
.crumb-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  padding: 3px 8px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-pill);
  background: none;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  cursor: pointer;
  transition:
    color var(--dcn-transition-fast),
    border-color var(--dcn-transition-fast);
}
.crumb-back:hover {
  border-color: var(--dcn-primary);
  color: var(--dcn-primary-light);
}
.crumb-trail {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.crumb {
  padding: 0;
  border: 0;
  background: none;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  cursor: pointer;
}
.crumb:hover {
  color: var(--dcn-primary-light);
}
.crumb.current {
  color: var(--dcn-text-primary);
  font-weight: 600;
  cursor: default;
}
.crumb-sep {
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-sm);
}
.department-list {
  flex: 1;
  max-height: 340px;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}
.department {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 38px;
  padding: 6px 10px;
  border: 0;
  border-radius: var(--dcn-radius-sm);
  background: none;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-base);
  text-align: left;
  cursor: pointer;
  transition: background var(--dcn-transition-fast);
}
.department:hover {
  background: var(--dcn-primary-bg);
}
.department .el-icon {
  color: var(--dcn-text-secondary);
}
.department-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.department-arrow {
  font-size: 12px;
}
.directory-main {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.directory-search {
  display: flex;
  align-items: center;
  gap: 10px;
}
.directory-search :deep(.el-input) {
  flex: 1;
}
.result-count {
  flex-shrink: 0;
  color: var(--dcn-text-secondary);
  font: var(--dcn-text-sm) var(--dcn-font-mono);
}
.user-list {
  flex: 1;
  max-height: 320px;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}
.user {
  display: flex;
  align-items: center;
  width: 100%;
  height: auto;
  min-height: 44px;
  margin-right: 0;
  padding: 6px 8px;
  border-radius: var(--dcn-radius-sm);
}
.user:hover {
  background: var(--dcn-primary-bg);
}
.user :deep(.el-checkbox__label) {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-left: 8px;
  line-height: 1.4;
}
.user-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.user-text strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-base);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.user-text small {
  overflow: hidden;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.directory-empty {
  padding: 22px 10px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  line-height: 1.6;
  text-align: center;
}
.selected-tray {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-right: auto;
  max-width: 560px;
  max-height: 76px;
  overflow: auto;
  padding-right: 8px;
}
.tray-label {
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
}
.tray-hint {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
}

/* ── 可访问性:键盘焦点必须有可见指示 ── */
.crumb:focus-visible,
.crumb-back:focus-visible,
.department:focus-visible,
.chip-remove:focus-visible {
  outline: 2px solid var(--dcn-focus-ring);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .webhook-card,
  .chip-remove,
  .department,
  .crumb-back {
    transition: none;
  }
  .webhook-card:hover {
    transform: none;
  }
}

@media (max-width: 720px) {
  .panel-head {
    flex-direction: column;
    align-items: stretch;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
  .webhook-grid {
    grid-template-columns: 1fr;
  }
  .directory {
    grid-template-columns: 1fr;
  }
  .directory-side {
    padding: 0 0 10px;
    border-right: 0;
    border-bottom: 1px solid var(--dcn-border-light);
  }
  .manual-add {
    flex-direction: column;
  }
}
</style>

<style>
/* 弹窗会被 teleport 到 body，scoped 的 :deep() 覆盖不到外壳，这里用独立类名做非作用域样式 */
.dcn-webhook-dialog .el-dialog__body,
.dcn-directory-dialog .el-dialog__body {
  padding-top: 8px;
}
.dcn-webhook-dialog .el-dialog__footer,
.dcn-directory-dialog .el-dialog__footer {
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
.dcn-directory-dialog .el-dialog__body {
  max-height: 70vh;
  overflow: auto;
}
</style>
