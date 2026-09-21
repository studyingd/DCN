<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">业务监控</h2>
      <span class="biz-desc">以业务为视角 · 业务 → 服务器 / 接口 健康聚合</span>
    </div>

    <el-alert v-if="pollError" :title="pollError" type="warning" show-icon :closable="false" class="poll-alert" />
    <div class="biz-body">
      <div class="biz-toolbar">
        <el-input v-model="keyword" placeholder="搜索业务" prefix-icon="Search" clearable class="biz-search" />
        <div class="biz-toolbar-right">
          <span class="biz-updated" :class="{ 'is-stale': isStale(lastLoaded) }">
            {{ lastLoaded ? `数据时间 ${fmtTime(lastLoaded)}` : '暂无数据' }}
            <el-tag v-if="isStale(lastLoaded)" size="small" type="warning" effect="plain">数据可能已过期</el-tag>
          </span>
          <el-button :loading="loading" @click="loadOverview"
            ><el-icon><Refresh /></el-icon>刷新</el-button
          >
          <el-button v-if="canManage" type="primary" @click="openBizDialog(null)">
            <el-icon><Plus /></el-icon>新建业务
          </el-button>
        </div>
      </div>

      <div v-loading="loading && businesses.length === 0" class="biz-list">
        <el-empty
          v-if="!loading && filteredBusinesses.length === 0"
          description="暂无业务,点击右上角新建"
          :image-size="70"
        />

        <div v-else class="biz-grid">
          <div
            v-for="b in filteredBusinesses"
            :key="b.id"
            v-memo="[
              b.id,
              b.name,
              b.description,
              b.health,
              b.server_online,
              b.server_total,
              b.interface_up,
              b.interface_total,
            ]"
            class="biz-card"
            :class="healthAccent(b.health)"
            role="button"
            tabindex="0"
            :aria-label="`打开业务 ${b.name} 详情`"
            @click="openDetail(b)"
            @keydown.enter="openDetail(b)"
            @keydown.space.prevent="openDetail(b)"
          >
            <div class="biz-card-top">
              <span class="biz-dot" :class="healthAccent(b.health)"></span>
              <span class="biz-name" :title="b.name">{{ b.name }}</span>
              <el-tag size="small" :type="healthTagType(b.health)" effect="plain">{{ healthLabel(b.health) }}</el-tag>
            </div>
            <div class="biz-card-desc" :title="b.description || ''">{{ b.description || '无描述' }}</div>
            <div class="biz-card-stats">
              <div class="biz-stat">
                <el-icon><Monitor /></el-icon>
                <span>服务器</span>
                <span
                  class="biz-stat-val"
                  :class="{ ok: b.server_online === b.server_total, bad: b.server_online < b.server_total }"
                >
                  {{ b.server_online }}/{{ b.server_total }}
                </span>
              </div>
              <div class="biz-stat">
                <el-icon><Connection /></el-icon>
                <span>接口</span>
                <span
                  class="biz-stat-val"
                  :class="{ ok: b.interface_up === b.interface_total, bad: b.interface_up < b.interface_total }"
                >
                  {{ b.interface_up }}/{{ b.interface_total }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ══════════ 业务详情抽屉 ══════════ -->
    <el-drawer v-model="detailVisible" :title="detail?.name || '业务详情'" size="640px" direction="rtl">
      <div v-loading="detailLoading" class="biz-detail">
        <template v-if="detail">
          <div class="biz-detail-health">
            <el-tag :type="healthTagType(detail.health)" size="large" effect="plain">{{
              healthLabel(detail.health)
            }}</el-tag>
            <span class="biz-detail-desc">{{ detail.description || '无描述' }}</span>
            <span v-if="canManage" class="biz-detail-actions">
              <el-button link type="primary" size="small" @click="openBizDialog(detail)">重命名</el-button>
              <el-button link type="danger" size="small" @click="removeBusiness(detail)">删除</el-button>
            </span>
          </div>

          <div class="biz-detail-section">
            <div class="biz-detail-title">
              <span>服务器 ({{ detail.server_online }}/{{ detail.server_total }} 在线)</span>
              <el-button v-if="canManage" size="small" type="primary" plain @click="openAddServer"
                >添加服务器</el-button
              >
            </div>
            <div class="biz-entries">
              <div v-for="s in detail.servers" :key="entryKey(s)" class="biz-entry">
                <span class="status-dot" :class="entryDotClass(s)" />
                <span class="biz-entry-name" :title="entryTitle(s)">{{ s.name }}</span>
                <span class="biz-entry-ip">{{ entrySubLabel(s) }}</span>
                <el-tag size="small" :type="entryTagType(s)" effect="plain">{{ entryStatusLabel(s) }}</el-tag>
                <el-button
                  v-if="canRemoveEntry(s)"
                  link
                  type="danger"
                  size="small"
                  class="biz-entry-del"
                  @click="unlinkEntry(s)"
                  >移除</el-button
                >
              </div>
              <el-empty v-if="detail.servers.length === 0" description="暂无关联服务器" :image-size="40" />
            </div>
          </div>

          <div class="biz-detail-section">
            <div class="biz-detail-title">
              <span>接口 ({{ detail.interface_up }}/{{ detail.interface_total }} 正常)</span>
              <el-button v-if="canManage" size="small" type="primary" plain @click="openAddIface">新建接口</el-button>
            </div>
            <div class="biz-entries">
              <div v-for="i in detail.interfaces" :key="i.interface_id" class="biz-entry">
                <span
                  class="status-dot"
                  :class="i.enabled !== 1 ? 'dot-maintenance' : i.up === 1 ? 'dot-online' : 'dot-offline'"
                />
                <span class="biz-entry-name" :title="i.url">{{ i.name }}</span>
                <span class="biz-entry-latency">{{ i.latency_ms != null ? i.latency_ms + 'ms' : '-' }}</span>
                <span class="biz-entry-code">{{ i.status_code ?? '-' }}</span>
                <el-tooltip :content="i.error || ''" :disabled="!i.error" placement="top" :show-after="300">
                  <el-tag
                    size="small"
                    :type="i.enabled !== 1 ? 'info' : i.up === 1 ? 'success' : 'danger'"
                    effect="plain"
                    >{{ i.enabled !== 1 ? '已停用' : i.up === 1 ? '正常' : '异常' }}</el-tag
                  >
                </el-tooltip>
                <el-button
                  v-if="canManage"
                  link
                  type="primary"
                  size="small"
                  class="biz-entry-del"
                  @click="openEditIface(i)"
                  >编辑</el-button
                >
                <el-button
                  v-if="canManage"
                  link
                  type="danger"
                  size="small"
                  class="biz-entry-del"
                  @click="unlinkInterface(i.interface_id)"
                  >移除</el-button
                >
              </div>
              <el-empty v-if="detail.interfaces.length === 0" description="暂无关联接口" :image-size="40" />
            </div>
          </div>
        </template>
      </div>
    </el-drawer>

    <!-- ══════════ 业务编辑弹窗 ══════════ -->
    <el-dialog v-model="bizDialogVisible" :title="bizEditing ? '编辑业务' : '新建业务'" width="440px" append-to-body>
      <el-form label-width="70px">
        <el-form-item label="名称" required>
          <el-input v-model="bizForm.name" placeholder="如:订单系统" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="bizForm.description" type="textarea" :rows="3" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="bizDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="bizSaving" @click="saveBusiness">保存</el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 接口弹窗(新建并关联 / 编辑) ══════════ -->
    <el-dialog
      v-model="ifaceDialogVisible"
      :title="ifaceDialogMode === 'edit' ? '编辑接口' : '新建接口并关联'"
      width="560px"
      append-to-body
    >
      <el-form label-width="100px">
        <el-form-item label="接口名" required>
          <el-input v-model="ifaceForm.name" placeholder="如:登录接口" />
        </el-form-item>
        <el-form-item label="URL" required>
          <el-input v-model="ifaceForm.url" placeholder="https://api.example.com/health" />
        </el-form-item>
        <el-form-item label="HTTP 方法">
          <el-select v-model="ifaceForm.method" style="width: 140px">
            <el-option
              v-for="m in ['GET', 'POST', 'HEAD', 'PUT', 'DELETE', 'OPTIONS']"
              :key="m"
              :label="m"
              :value="m"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="预期状态码">
          <el-input-number v-model="ifaceForm.expected_status" :controls="false" :min="100" :max="599" />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="ifaceForm.timeout" :controls="false" :min="1" :max="60" />
        </el-form-item>
        <el-form-item label="启用探测">
          <el-switch v-model="ifaceForm.enabledBool" />
        </el-form-item>
        <el-form-item label="请求头">
          <el-input
            v-model="ifaceForm.headersText"
            type="textarea"
            :rows="2"
            placeholder='JSON，例如 {"X-API-Key":"..."}'
          />
        </el-form-item>
        <el-form-item label="认证方式">
          <el-select v-model="ifaceForm.auth_type" style="width: 150px">
            <el-option label="无" value="none" />
            <el-option label="Basic" value="basic" />
            <el-option label="Bearer Token" value="bearer" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="ifaceForm.auth_type === 'basic'" label="用户名"
          ><el-input v-model="ifaceForm.auth_username"
        /></el-form-item>
        <el-form-item v-if="ifaceForm.auth_type === 'basic'" label="密码"
          ><el-input v-model="ifaceForm.auth_password" type="password" show-password placeholder="留空表示保持原密码"
        /></el-form-item>
        <el-form-item v-if="ifaceForm.auth_type === 'bearer'" label="Token"
          ><el-input v-model="ifaceForm.auth_token" type="password" show-password placeholder="留空表示保持原 Token"
        /></el-form-item>
        <el-form-item label="请求体"
          ><el-input v-model="ifaceForm.request_body" type="textarea" :rows="3" placeholder="可选，POST/PUT 等请求体"
        /></el-form-item>
        <el-form-item label="响应校验"
          ><el-input v-model="ifaceForm.response_contains" placeholder="响应文本必须包含的内容"
        /></el-form-item>
        <el-form-item label="描述">
          <el-input v-model="ifaceForm.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ifaceDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="ifaceSaving" @click="saveIface">
          {{ ifaceDialogMode === 'edit' ? '保存' : '创建并关联' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 添加服务器弹窗(机房设备 + PVE 虚拟机/容器) ══════════ -->
    <!-- ══════════ 添加服务器弹窗(机房分组+虚拟化卡片,与告警规则选择监控对象同一交互) ══════════ -->
    <el-dialog
      v-model="addServerVisible"
      title="添加服务器到业务"
      width="860px"
      append-to-body
      class="add-server-dialog"
      :close-on-click-modal="false"
    >
      <DeviceSelector
        :selected-ids="addServerIds"
        :selectable-fn="addServerSelectable"
        :load-guests="loadGuestsForSelector"
        @confirm="onAddServerConfirm"
      />
      <template #footer>
        <el-button @click="addServerVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="addServerSaving"
          :disabled="addServerIds.length === 0"
          @click="saveAddServer"
          >添加（已选 {{ addServerIds.length }} 台）</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Monitor, Connection } from '@element-plus/icons-vue'
import { businessAPI, interfaceAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import type {
  BusinessDetail,
  BusinessHealthItem,
  InterfaceInBusiness,
  PveGuestCandidate,
  ServerInBusiness,
} from '@/types/business'

const authStore = useAuthStore()
const canManage = computed(() => authStore.hasPermission('device:manage'))
const canManageGuests = computed(() => canManage.value && authStore.hasPermission('pve:manage'))

const loading = ref(false)
const businesses = ref<BusinessHealthItem[]>([])
const keyword = ref('')
const lastLoaded = ref<string | null>(null)
const pollError = ref('')
let pollTimer: number | undefined
let overviewRequest: Promise<void> | null = null

const filteredBusinesses = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return businesses.value
  return businesses.value.filter((b) => b.name.toLowerCase().includes(q))
})

onMounted(() => {
  loadOverview()
  pollTimer = window.setInterval(() => {
    if (!document.hidden) void loadOverview(true)
  }, 30000)
})

onUnmounted(() => window.clearInterval(pollTimer))

async function loadOverview(quiet = false) {
  if (overviewRequest) return overviewRequest
  const request = (async () => {
    if (!quiet) loading.value = true
    try {
      const res = await businessAPI.list()
      businesses.value = res.data
      lastLoaded.value = new Date().toISOString()
      pollError.value = ''
    } catch {
      pollError.value = '业务监控数据刷新失败，当前仍显示上一次成功数据，请手动重试'
      if (!quiet) ElMessage.error('加载业务列表失败')
    } finally {
      if (!quiet) loading.value = false
    }
  })()
  overviewRequest = request
  try {
    await request
  } finally {
    if (overviewRequest === request) overviewRequest = null
  }
}

function isStale(ts: string | null, maxAgeSeconds = 120): boolean {
  return !!ts && Date.now() - new Date(ts).getTime() > maxAgeSeconds * 1000
}

// ── 健康状态 ──
function healthLabel(h: string): string {
  return { healthy: '健康', degraded: '部分异常', down: '异常', unknown: '无数据' }[h] || h
}
function healthTagType(h: string): string {
  return { healthy: 'success', degraded: 'warning', down: 'danger', unknown: 'info' }[h] || 'info'
}
function healthAccent(h: string): string {
  return (
    { healthy: 'accent-healthy', degraded: 'accent-degraded', down: 'accent-down', unknown: 'accent-unknown' }[h] ||
    'accent-unknown'
  )
}
// ── 服务器条目(设备与 PVE 虚拟机统一渲染;PVE 连接不可达时显示"状态未知") ──
function entryKey(s: ServerInBusiness): string {
  return s.kind === 'pve' ? `pve:${s.link_id}` : `device:${s.device_id}`
}
function entryUnknown(s: ServerInBusiness): boolean {
  return s.kind === 'pve' && s.reachable !== 1
}
function entryDotClass(s: ServerInBusiness): string {
  if (entryUnknown(s)) return 'dot-maintenance'
  return s.online === 1 ? 'dot-online' : 'dot-offline'
}
function entryTagType(s: ServerInBusiness): string {
  if (entryUnknown(s)) return 'info'
  return s.online === 1 ? 'success' : 'danger'
}
function entryStatusLabel(s: ServerInBusiness): string {
  if (s.kind !== 'pve') return s.online === 1 ? '在线' : '离线'
  if (entryUnknown(s)) return '状态未知'
  if (s.status === 'running') return '运行中'
  if (s.status === 'stopped') return '已停止'
  return '状态未知'
}
function entrySubLabel(s: ServerInBusiness): string {
  if (s.kind !== 'pve') return s.ip_address || '-'
  if (s.ip_address) return s.ip_address
  return s.node ? `${s.node} · ${s.vmid}` : `${s.connection_name || 'PVE'} · ${s.vmid}`
}
function entryTitle(s: ServerInBusiness): string {
  if (s.kind !== 'pve') return s.ip_address ? `${s.name} · ${s.ip_address}` : s.name
  return `${s.connection_name || 'PVE'} · ${s.guest_type}/${s.vmid}${s.node ? ` · ${s.node}` : ''}`
}
function canRemoveEntry(s: ServerInBusiness): boolean {
  return s.kind === 'pve' ? canManageGuests.value : canManage.value
}

// ── 业务 CRUD ──
const bizDialogVisible = ref(false)
const bizSaving = ref(false)
const bizEditing = ref<BusinessHealthItem | null>(null)
const bizForm = reactive({ name: '', description: '' })

function openBizDialog(b: BusinessHealthItem | null) {
  bizEditing.value = b
  bizForm.name = b?.name || ''
  bizForm.description = b?.description || ''
  bizDialogVisible.value = true
}

async function saveBusiness() {
  if (!bizForm.name.trim()) {
    ElMessage.warning('请输入业务名称')
    return
  }
  bizSaving.value = true
  try {
    if (bizEditing.value) {
      await businessAPI.update(bizEditing.value.id, { name: bizForm.name, description: bizForm.description })
      ElMessage.success('业务已更新')
    } else {
      await businessAPI.create({ name: bizForm.name, description: bizForm.description })
      ElMessage.success('业务已创建')
    }
    bizDialogVisible.value = false
    await loadOverview(true)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '保存失败')
  } finally {
    bizSaving.value = false
  }
}

async function removeBusiness(b: BusinessHealthItem) {
  try {
    await ElMessageBox.confirm(`确定删除业务「${b.name}」?`, '删除确认', { type: 'warning' })
    await businessAPI.remove(b.id)
    ElMessage.success('业务已删除')
    // 从详情抽屉里删除时同步关抽屉,避免展示已不存在的业务详情。
    if (detail.value?.id === b.id) {
      detailVisible.value = false
      detail.value = null
    }
    await loadOverview(true)
  } catch {
    /* cancelled or error */
  }
}

// ── 业务详情 ──
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<BusinessDetail | null>(null)
let detailRequestId = 0
let detailPollTimer: number | undefined

function applyDetail(nextDetail: BusinessDetail) {
  detail.value = nextDetail
  const index = businesses.value.findIndex((business) => business.id === nextDetail.id)
  if (index === -1) return
  businesses.value[index] = {
    ...businesses.value[index],
    name: nextDetail.name,
    description: nextDetail.description,
    server_total: nextDetail.server_total,
    server_online: nextDetail.server_online,
    interface_total: nextDetail.interface_total,
    interface_up: nextDetail.interface_up,
    health: nextDetail.health,
  }
}

// 抽屉打开期间周期刷新(与列表轮询同 30s):接口 up/延迟/状态在
// 后台 60s 探测周期里变化，不刷新的话要关开抽屉才能看到新状态。
// 静默轮询(quiet):失败不弹错不打 loading，保持旧数据。
watch(detailVisible, (visible) => {
  window.clearInterval(detailPollTimer)
  if (visible) {
    detailPollTimer = window.setInterval(() => {
      if (!document.hidden && detail.value && !detailLoading.value) {
        void fetchDetail(detail.value.id, true)
      }
    }, 30000)
  } else {
    detailPollTimer = undefined
  }
})
onUnmounted(() => window.clearInterval(detailPollTimer))

async function fetchDetail(businessId: number, quiet = false) {
  const requestId = ++detailRequestId
  if (!quiet) detailLoading.value = true
  try {
    const res = await businessAPI.detail(businessId)
    if (requestId === detailRequestId) applyDetail(res.data)
  } catch {
    if (!quiet) ElMessage.error('加载业务详情失败')
  } finally {
    if (requestId === detailRequestId && !quiet) detailLoading.value = false
  }
}

function openDetail(b: BusinessHealthItem) {
  detailVisible.value = true
  void fetchDetail(b.id)
}

// ── 接口:新建并关联 / 编辑(复用同一表单) ──
const ifaceDialogVisible = ref(false)
const ifaceSaving = ref(false)
const ifaceDialogMode = ref<'create-link' | 'edit'>('create-link')
const editingIfaceId = ref<number | null>(null)
const ifaceForm = reactive({
  name: '',
  url: '',
  method: 'GET',
  expected_status: 200,
  timeout: 10,
  enabledBool: true,
  description: '',
  headersText: '{}',
  auth_type: 'none',
  auth_username: '',
  auth_password: '',
  auth_token: '',
  request_body: '',
  response_contains: '',
})

function openAddIface() {
  ifaceDialogMode.value = 'create-link'
  editingIfaceId.value = null
  Object.assign(ifaceForm, {
    name: '',
    url: '',
    method: 'GET',
    expected_status: 200,
    timeout: 10,
    enabledBool: true,
    description: '',
    headersText: '{}',
    auth_type: 'none',
    auth_username: '',
    auth_password: '',
    auth_token: '',
    request_body: '',
    response_contains: '',
  })
  ifaceDialogVisible.value = true
}

function openEditIface(i: InterfaceInBusiness) {
  ifaceDialogMode.value = 'edit'
  editingIfaceId.value = i.interface_id
  Object.assign(ifaceForm, {
    name: i.name,
    url: i.url,
    method: i.method,
    expected_status: i.expected_status,
    timeout: i.timeout,
    enabledBool: !!i.enabled,
    description: i.description || '',
    headersText: JSON.stringify(i.request_headers || {}, null, 2),
    auth_type: i.auth_type || 'none',
    auth_username: i.auth_username || '',
    auth_password: '',
    auth_token: '',
    request_body: i.request_body || '',
    response_contains: i.response_contains || '',
  })
  ifaceDialogVisible.value = true
}

async function saveIface() {
  if (!ifaceForm.name.trim() || !ifaceForm.url.trim()) {
    ElMessage.warning('请填写接口名和 URL')
    return
  }
  ifaceSaving.value = true
  let request_headers: Record<string, string>
  try {
    const parsed = JSON.parse(ifaceForm.headersText || '{}')
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error()
    request_headers = Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)]))
  } catch {
    ElMessage.warning('请求头必须是合法 JSON 对象')
    ifaceSaving.value = false
    return
  }
  const payload = {
    name: ifaceForm.name,
    url: ifaceForm.url,
    method: ifaceForm.method,
    expected_status: ifaceForm.expected_status,
    timeout: ifaceForm.timeout,
    enabled: ifaceForm.enabledBool ? 1 : 0,
    description: ifaceForm.description,
    request_headers,
    auth_type: ifaceForm.auth_type,
    auth_username: ifaceForm.auth_username || undefined,
    auth_password: ifaceForm.auth_password || undefined,
    auth_token: ifaceForm.auth_token || undefined,
    request_body: ifaceForm.request_body || undefined,
    response_contains: ifaceForm.response_contains || undefined,
  }
  try {
    if (ifaceDialogMode.value === 'edit' && editingIfaceId.value != null) {
      await interfaceAPI.update(editingIfaceId.value, payload)
      if (detail.value) {
        const interfaceId = editingIfaceId.value
        detail.value = {
          ...detail.value,
          interfaces: detail.value.interfaces.map((item) =>
            item.interface_id === interfaceId
              ? {
                  ...item,
                  name: payload.name,
                  url: payload.url,
                  method: payload.method,
                  expected_status: payload.expected_status,
                  timeout: payload.timeout,
                  enabled: payload.enabled,
                  description: payload.description,
                }
              : item,
          ),
        }
      }
      ElMessage.success('接口已更新')
    } else {
      if (!detail.value) throw new Error('未选择业务')
      const res = await businessAPI.createInterface(detail.value.id, payload)
      applyDetail(res.data)
      ElMessage.success('接口已创建并关联')
    }
    ifaceDialogVisible.value = false
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '保存失败')
  } finally {
    ifaceSaving.value = false
  }
}

// ── 添加服务器(机房设备 + PVE 虚拟机/容器，同一个选择器) ──
type GuestLinkItem = { connection_id: number; guest_type: string; vmid: number; name?: string }

// 与后端一致的虚拟机合成 id 公式(仅用于选择器:设备正数 id 与虚拟机负数
// id 混在同一选择集里;拆回关联载荷时反查 guestCandidates)。
const PVE_ID_FACTOR = 1_000_000

const addServerVisible = ref(false)
const addServerSaving = ref(false)
const addServerIds = ref<number[]>([])
const guestCandidates = ref<PveGuestCandidate[]>([])

/** 合成 id -> 候选项(拆关联载荷时取 connection_id/guest_type/vmid/name)。 */
const guestIndex = computed(() => {
  const map = new Map<number, PveGuestCandidate>()
  for (const c of guestCandidates.value) {
    map.set(-(c.connection_id * PVE_ID_FACTOR + c.vmid), c)
  }
  return map
})

/** DeviceSelector 的虚拟机候选注入:业务面板只需 pve 权限(不必
 * 拿 automation 权限),走业务自己的候选接口并映射成统一结构。 */
async function loadGuestsForSelector(): Promise<
  Array<{
    id: number
    connection_id: number
    guest_type: string
    vmid: number
    name: string
    node: string | null
    ip_address: string | null
  }>
> {
  try {
    const res = await businessAPI.guestCandidates()
    guestCandidates.value = res.data
    return res.data.map((c) => ({
      id: -(c.connection_id * PVE_ID_FACTOR + c.vmid),
      connection_id: c.connection_id,
      guest_type: c.guest_type,
      vmid: c.vmid,
      name: c.name,
      node: c.node,
      ip_address: c.ip_address,
    }))
  } catch {
    guestCandidates.value = []
    return []
  }
}

// 已关联目标置灰不可再选(后端虽会幂等跳过,置灰更直观)。
function addServerSelectable(id: number, isGuest: boolean): boolean {
  if (isGuest || id < 0) return !linkedGuestIds.value.has(id)
  return !linkedDeviceIds.value.has(id)
}

const linkedDeviceIds = computed(() => {
  const ids = new Set<number>()
  for (const s of detail.value?.servers || []) {
    if (s.kind !== 'pve' && s.device_id != null) ids.add(s.device_id)
  }
  return ids
})
const linkedGuestIds = computed(() => {
  const ids = new Set<number>()
  for (const s of detail.value?.servers || []) {
    if (s.kind === 'pve' && s.connection_id != null && s.vmid != null) {
      ids.add(-(s.connection_id * PVE_ID_FACTOR + s.vmid))
    }
  }
  return ids
})

function onAddServerConfirm(ids: number[]) {
  addServerIds.value = ids
}

function openAddServer() {
  addServerIds.value = []
  addServerVisible.value = true
}

function splitSelection(ids: number[]): { deviceIds: number[]; guests: GuestLinkItem[] } {
  const deviceIds: number[] = []
  const guests: GuestLinkItem[] = []
  for (const id of ids) {
    if (id > 0) {
      deviceIds.push(id)
      continue
    }
    const matched = guestIndex.value.get(id)
    if (!matched) continue
    guests.push({
      connection_id: matched.connection_id,
      guest_type: matched.guest_type,
      vmid: matched.vmid,
      name: matched.name,
    })
  }
  return { deviceIds, guests }
}

function errorDetail(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: string } } }
  return err.response?.data?.detail || fallback
}

async function saveAddServer() {
  if (!detail.value) return
  const { deviceIds, guests } = splitSelection(addServerIds.value)
  if (deviceIds.length === 0 && guests.length === 0) return
  const businessId = detail.value.id
  const failed: string[] = []
  let latest: BusinessDetail | null = null
  addServerSaving.value = true
  try {
    // 设备与虚拟机分属两个接口:任一成功都要刷新详情，失败原因如实提示。
    if (deviceIds.length > 0) {
      try {
        latest = (await businessAPI.linkServers(businessId, deviceIds)).data
      } catch (e: unknown) {
        failed.push(errorDetail(e, '关联设备失败'))
      }
    }
    if (guests.length > 0) {
      try {
        latest = (await businessAPI.linkGuests(businessId, guests)).data
      } catch (e: unknown) {
        failed.push(errorDetail(e, '关联虚拟机失败'))
      }
    }
  } finally {
    addServerSaving.value = false
  }
  if (latest) applyDetail(latest)
  if (failed.length > 0) {
    ElMessage.error(failed.join('；'))
    return
  }
  ElMessage.success(`已添加 ${deviceIds.length + guests.length} 台服务器`)
  addServerVisible.value = false
}

async function unlinkEntry(s: ServerInBusiness) {
  if (!detail.value) return
  const businessId = detail.value.id
  try {
    if (s.kind === 'pve') {
      if (s.link_id == null) return
      applyDetail((await businessAPI.unlinkGuest(businessId, s.link_id)).data)
    } else {
      if (s.device_id == null) return
      applyDetail((await businessAPI.unlinkServer(businessId, s.device_id)).data)
    }
    ElMessage.success('已移除关联')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '移除关联失败')
  }
}

async function unlinkInterface(interfaceId: number) {
  if (!detail.value) return
  try {
    const res = await businessAPI.unlinkInterface(detail.value.id, interfaceId)
    applyDetail(res.data)
    ElMessage.success('已移除关联')
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '移除关联失败')
  }
}

function fmtTime(ts: string | null): string {
  if (!ts) return '-'
  const d = new Date(ts)
  const pad = (n: number) => (n < 10 ? `0${n}` : String(n))
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
</script>

<style scoped>
.biz-desc {
  font-size: 13px;
  color: var(--dcn-text-secondary);
}

.biz-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-4) var(--dcn-space-6);
}

.biz-toolbar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
}
.biz-search {
  width: 240px;
}
.biz-toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.biz-updated {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

/* ── 业务卡片 ── */
.biz-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--dcn-space-3);
}
.biz-card {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-3);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  cursor: pointer;
  transition:
    box-shadow var(--dcn-transition-fast),
    transform var(--dcn-transition-fast);
  color: inherit;
  font: inherit;
  text-align: left;
}
.biz-card:hover {
  border-color: var(--dcn-border-strong);
  box-shadow: var(--dcn-shadow-md);
  transform: translateY(-1px);
}
.biz-card:focus-visible {
  outline: 2px solid var(--dcn-primary);
  outline-offset: 2px;
}

@media (max-width: 900px) {
  .biz-body {
    padding: var(--dcn-space-4);
  }
  .biz-toolbar {
    flex-wrap: wrap;
  }
  .biz-toolbar-right {
    width: 100%;
    margin-left: 0;
    justify-content: space-between;
  }
}
.biz-card-top {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.biz-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.biz-dot.accent-healthy {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 6px var(--dcn-dot-online);
}
.biz-dot.accent-degraded {
  background: var(--dcn-warning);
  box-shadow: 0 0 6px var(--dcn-warning);
}
.biz-dot.accent-down {
  background: var(--dcn-dot-offline);
}
.biz-dot.accent-unknown {
  background: var(--dcn-border-strong);
}
.biz-name {
  flex: 1;
  font-weight: 600;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.biz-card-desc {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.biz-card-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--dcn-space-4);
}
.biz-stat {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
}
.biz-stat-val {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.biz-stat-val.ok {
  color: var(--dcn-dot-online);
}
.biz-stat-val.bad {
  color: var(--dcn-dot-offline);
}

/* ── 业务详情 ── */
.biz-detail {
  padding: 0 var(--dcn-space-1);
}
.biz-detail-health {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.biz-detail-desc {
  flex: 1;
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.biz-detail-actions {
  flex-shrink: 0;
  display: flex;
  gap: var(--dcn-space-1);
}
.biz-detail-section {
  margin-bottom: var(--dcn-space-4);
}
.biz-detail-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin-bottom: var(--dcn-space-2);
}
.biz-entries {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}
.biz-entry {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) var(--dcn-space-3);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
}
.biz-entry-name {
  flex: 1;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.biz-entry-ip {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
}
.biz-entry-latency,
.biz-entry-code {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  font-variant-numeric: tabular-nums;
  width: 52px;
  text-align: right;
}
.biz-entry-del {
  flex-shrink: 0;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-online {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 6px var(--dcn-dot-online);
}
.dot-offline {
  background: var(--dcn-dot-offline);
}
.dot-maintenance {
  background: var(--dcn-dot-maintenance);
}
</style>
