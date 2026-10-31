<template>
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
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import { ArrowRight, Back, Close, OfficeBuilding, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { webhookAPI } from '@/api'
import type { FeishuDepartment, FeishuDirectoryRequest, FeishuUser } from '@/types'
import { MAX_RECEIVERS } from './webhookMeta'
import type { DirectoryOpenContext } from './webhookMeta'

const emit = defineEmits<{ (e: 'apply', receivers: string[], names: Record<string, string>): void }>()

/** 打开时冻结的表单上下文:凭据 + 现有接收人快照(弹窗打开期间表单被遮罩挡住,快照即实时值) */
let ctx: DirectoryOpenContext | null = null

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
  if (!ctx) return null
  const appId = ctx.appId.trim()
  if (!appId) {
    ElMessage.warning('请先填写 App ID')
    return null
  }
  // 编辑已保存的通道时 Secret 可以留空，后端回落到库里加密存的那份
  if (!ctx.appSecret && !ctx.secretSet) {
    ElMessage.warning('请先填写 App Secret 再读取通讯录')
    return null
  }
  return { webhook_id: ctx.webhookId, base: ctx.base, app_id: appId, app_secret: ctx.appSecret || undefined }
}
function currentDepartmentId() {
  return directory.trail.length ? directory.trail[directory.trail.length - 1].id : '0'
}
/** 记下姓名，并把"已经在接收人列表里的人"回显成勾选状态 */
function absorbUsers(users: FeishuUser[]) {
  for (const user of users) {
    directory.names[user.open_id] = user.name
    const identities = [user.open_id, user.user_id, user.union_id, user.email, user.enterprise_email].filter(Boolean)
    const hit = (ctx?.receivers || []).find((item) => identities.includes(item))
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
async function open(context: DirectoryOpenContext) {
  ctx = context
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
  if (!ctx) return
  const unchecked = Object.entries(directory.matched)
    .filter(([openId]) => !directory.selected.includes(openId))
    .map(([, stored]) => stored)
  const kept = ctx.receivers.filter((item) => !unchecked.includes(item))
  const merged = [...kept]
  const names: Record<string, string> = {}
  for (const [id, name] of Object.entries(ctx.receiverNames)) if (kept.includes(id)) names[id] = name
  for (const openId of directory.selected) {
    // 同一个人原本用邮箱存的，就继续用邮箱，避免重复投递
    const value = directory.matched[openId] || openId
    if (!merged.includes(value)) merged.push(value)
    const name = directory.names[openId]
    if (name) names[value] = name
  }
  const receivers = merged.slice(0, MAX_RECEIVERS)
  const mergedNames = Object.fromEntries(Object.entries(names).filter(([id]) => receivers.includes(id)))
  directory.visible = false
  ElMessage.success(
    unchecked.length
      ? `已写入 ${directory.selected.length} 位接收人，移除 ${unchecked.length} 位`
      : `已写入 ${directory.selected.length} 位接收人`,
  )
  emit('apply', receivers, mergedNames)
}
function errDetail(e: unknown, fallback: string) {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
}

defineExpose({ open })
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

/* ── 接收人芯片(已选托盘) ── */
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

/* ── 可访问性:键盘焦点必须有可见指示 ── */
.crumb:focus-visible,
.crumb-back:focus-visible,
.department:focus-visible,
.chip-remove:focus-visible {
  outline: 2px solid var(--dcn-focus-ring);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .chip-remove,
  .department,
  .crumb-back {
    transition: none;
  }
}

@media (max-width: 720px) {
  .directory {
    grid-template-columns: 1fr;
  }
  .directory-side {
    padding: 0 0 10px;
    border-right: 0;
    border-bottom: 1px solid var(--dcn-border-light);
  }
}
</style>

<style>
/* 弹窗会被 teleport 到 body，scoped 的 :deep() 覆盖不到外壳，这里用独立类名做非作用域样式 */
.dcn-directory-dialog .el-dialog__body {
  padding-top: 8px;
  max-height: 70vh;
  overflow: auto;
}
.dcn-directory-dialog .el-dialog__footer {
  display: flex;
  align-items: center;
  gap: 10px;
}
</style>
