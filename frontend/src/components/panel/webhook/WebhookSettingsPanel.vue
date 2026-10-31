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

    <WebhookFormDialog ref="formDialog" @saved="onSaved" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Bell,
  CircleCheck,
  CircleClose,
  Delete,
  Edit,
  Plus,
  Promotion,
  Refresh,
  UserFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { webhookAPI } from '@/api'
import type { Webhook } from '@/types'
import { formatDateTime } from '@/utils/datetime'
import WebhookFormDialog from './WebhookFormDialog.vue'
import { chipOf, eventLabel, providerIcon, providerLabel } from './webhookMeta'
import type { ReceiverChip } from './webhookMeta'

const webhooks = ref<Webhook[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const formDialog = ref<InstanceType<typeof WebhookFormDialog>>()

const onboardingSteps = [
  '选择通道类型：飞书私聊 / 群机器人，或企业微信、钉钉、自定义 HTTP',
  '填写接入信息：飞书自建应用填 App ID 与 App Secret，其它通道填 Webhook 地址',
  '指定接收人并勾选要推送的事件',
  '点「测试发送」，确认能收到样例告警卡片',
]

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

function testLabel(hook: Webhook) {
  return hook.last_test_at
    ? `${hook.last_test_status === 'success' ? '测试成功' : '测试失败'} · ${formatTime(hook.last_test_at)}`
    : '尚未测试'
}
function formatTime(value: string) {
  return formatDateTime(value)
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
  formDialog.value?.open(hook)
}
/** 表单弹窗保存成功:新建插入表头,编辑原位替换(与拆分前 submitForm 直写列表等价) */
function onSaved(hook: Webhook, wasEditing: boolean) {
  if (wasEditing) {
    const idx = webhooks.value.findIndex((item) => item.id === hook.id)
    if (idx >= 0) webhooks.value[idx] = hook
  } else {
    webhooks.value.unshift(hook)
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

/* ── 接收人芯片(卡片只读展示) ── */
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

@media (prefers-reduced-motion: reduce) {
  .webhook-card {
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
  .webhook-grid {
    grid-template-columns: 1fr;
  }
}
</style>
