<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">用户管理</h2>
    </div>
    <div class="inline-panel-body">
    <el-tabs v-model="activeTab">
      <!-- Tab 1: Users -->
      <el-tab-pane label="用户管理" name="users">
        <div style="margin-bottom: 12px;">
          <el-button type="primary" size="small" @click="openUserForm(null)">
            新建用户
          </el-button>
        </div>
        <el-table :data="users" v-loading="usersLoading" stripe size="small" max-height="420">
          <el-table-column prop="username" label="用户名" min-width="100" />
          <el-table-column prop="display_name" label="显示名" min-width="100">
            <template #default="{ row }">{{ row.display_name || '-' }}</template>
          </el-table-column>
          <el-table-column prop="role_name" label="角色" width="120">
            <template #default="{ row }">{{ row.role_name || '-' }}</template>
          </el-table-column>
          <el-table-column prop="group_name" label="分组" width="120">
            <template #default="{ row }">{{ row.group_name || '-' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80" align="center">
            <template #default="{ row }">
              <el-switch
                :model-value="!!row.is_active"
                size="small"
                @change="(val: boolean) => toggleUserActive(row, val)"
              />
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openUserForm(row)">编辑</el-button>
              <el-button
                v-if="authStore.user?.id !== row.id"
                link type="danger" size="small" @click="deleteUser(row)"
              >删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- Tab 2: Roles -->
      <el-tab-pane label="角色管理" name="roles">
        <div style="margin-bottom: 12px;">
          <el-button type="primary" size="small" @click="openRoleForm(null)">
            新建角色
          </el-button>
        </div>
        <el-table :data="roles" v-loading="rolesLoading" stripe size="small" max-height="420">
          <el-table-column prop="name" label="名称" min-width="120">
            <template #default="{ row }">
              <span>{{ row.name }}</span>
              <el-tag v-if="row.is_builtin" type="info" size="small" style="margin-left: 6px;">内置</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="140">
            <template #default="{ row }">{{ row.description || '-' }}</template>
          </el-table-column>
          <el-table-column label="权限" min-width="200">
            <template #default="{ row }">
              <el-tag
                v-for="permKey in (row.permissions || []).slice(0, 5)"
                :key="permKey"
                size="small"
                style="margin: 2px;"
              >{{ permissionLabel(permKey) }}</el-tag>
              <el-tag
                v-if="(row.permissions || []).length > 5"
                size="small"
                type="info"
                style="margin: 2px;"
              >+{{ row.permissions.length - 5 }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="设备范围" width="100" align="center">
            <template #default="{ row }">
              {{ row.device_scope === 'all' ? '全部设备' : '指定设备' }}
            </template>
          </el-table-column>
          <el-table-column prop="user_count" label="用户数" width="80" align="center" />
          <el-table-column label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openRoleForm(row)">编辑</el-button>
              <el-button
                v-if="!row.is_builtin"
                link type="danger" size="small" @click="deleteRole(row)"
              >删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- Tab 3: Groups -->
      <el-tab-pane label="用户分组" name="groups">
        <div style="margin-bottom: 12px;">
          <el-button type="primary" size="small" @click="openGroupForm(null)">
            新建分组
          </el-button>
        </div>
        <el-table :data="groups" v-loading="groupsLoading" stripe size="small" max-height="420">
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column prop="description" label="描述" min-width="200">
            <template #default="{ row }">{{ row.description || '-' }}</template>
          </el-table-column>
          <el-table-column prop="user_count" label="用户数" width="90" align="center" />
          <el-table-column label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openGroupForm(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="deleteGroup(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
    </div>

    <!-- Nested: User Form Dialog -->
    <el-dialog
      v-model="userFormVisible"
      :title="userFormIsEdit ? '编辑用户' : '新建用户'"
      width="500px"
      append-to-body
      :close-on-click-modal="false"
      @close="resetUserForm"
    >
      <el-form ref="userFormRef" :model="userFormData" :rules="userFormRules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="userFormData.username" placeholder="登录用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="userFormData.password"
            type="password"
            show-password
            :placeholder="userFormIsEdit ? '留空则不修改' : '请输入密码'"
          />
        </el-form-item>
        <el-form-item label="显示名" prop="display_name">
          <el-input v-model="userFormData.display_name" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色" prop="role_id">
          <el-select v-model="userFormData.role_id" placeholder="请选择角色" clearable style="width: 100%;">
            <el-option
              v-for="r in roles"
              :key="r.id"
              :label="r.name"
              :value="r.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="分组" prop="group_id">
          <el-select v-model="userFormData.group_id" placeholder="请选择分组" clearable style="width: 100%;">
            <el-option
              v-for="g in groups"
              :key="g.id"
              :label="g.name"
              :value="g.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="userFormData.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="userFormSubmitting" @click="submitUserForm">
          {{ userFormIsEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- Nested: Role Form Dialog -->
    <el-dialog
      v-model="roleFormVisible"
      :title="roleFormIsEdit ? '编辑角色' : '新建角色'"
      width="600px"
      append-to-body
      :close-on-click-modal="false"
      @close="resetRoleForm"
    >
      <el-form ref="roleFormRef" :model="roleFormData" :rules="roleFormRules" label-width="80px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="roleFormData.name" placeholder="角色名称" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="roleFormData.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
        <el-form-item label="权限">
          <el-checkbox-group v-model="roleFormData.permissions">
            <el-checkbox
              v-for="permKey in allPermissionKeys"
              :key="permKey"
              :label="permKey"
            >{{ permissionLabel(permKey) }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="设备范围">
          <el-radio-group v-model="roleFormData.device_scope" @change="onDeviceScopeChange">
            <el-radio value="all">全部设备</el-radio>
            <el-radio value="selected">指定设备</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="roleFormData.device_scope === 'selected'" label="选择设备">
          <el-button size="small" @click="openDeviceSelector">
            选择设备 (已选 {{ roleFormData.device_ids.length }} 台)
          </el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="roleFormSubmitting" @click="submitRoleForm">
          {{ roleFormIsEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- Nested: Group Form Dialog -->
    <el-dialog
      v-model="groupFormVisible"
      :title="groupFormIsEdit ? '编辑分组' : '新建分组'"
      width="480px"
      append-to-body
      :close-on-click-modal="false"
      @close="resetGroupForm"
    >
      <el-form ref="groupFormRef" :model="groupFormData" :rules="groupFormRules" label-width="80px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="groupFormData.name" placeholder="分组名称" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="groupFormData.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="groupFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="groupFormSubmitting" @click="submitGroupForm">
          {{ groupFormIsEdit ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- Nested: Device Selector Dialog -->
    <el-dialog
      v-model="deviceSelectorVisible"
      title="选择设备"
      width="900px"
      append-to-body
      :close-on-click-modal="false"
    >
      <div class="device-selector">
        <div class="device-selector-sidebar">
          <div class="device-selector-section-title">机房</div>
          <div
            v-for="room in selectorRooms"
            :key="room.id"
            class="device-selector-item"
            :class="{ active: selectedSelectorRoomId === room.id }"
            @click="selectSelectorRoom(room.id)"
          >{{ room.name }}</div>
          <el-empty v-if="selectorRooms.length === 0" description="暂无机房" :image-size="40" />
        </div>
        <div class="device-selector-main" v-if="selectedSelectorRoomId">
          <div class="device-selector-header">
            <el-checkbox
              v-model="selectorAllChecked"
              :indeterminate="selectorIndeterminate"
              @change="onSelectorAllChange"
            >全选 ({{ selectorDevices.length }} 台设备)</el-checkbox>
          </div>
          <div class="device-selector-body" v-loading="selectorDevicesLoading">
            <template v-for="rackGroup in selectorGroupedByRack" :key="rackGroup.rack_id">
              <div class="rack-group">
                <div class="rack-group-header">
                  <svg class="rack-icon" viewBox="0 0 16 16" width="14" height="14"><rect x="2" y="1" width="12" height="14" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/><line x1="5" y1="4" x2="11" y2="4" stroke="currentColor" stroke-width="1"/><line x1="5" y1="8" x2="11" y2="8" stroke="currentColor" stroke-width="1"/><line x1="5" y1="12" x2="11" y2="12" stroke="currentColor" stroke-width="1"/></svg>
                  <span class="rack-group-name">{{ rackGroup.rack_name }}</span>
                  <span class="rack-group-count">{{ rackGroup.devices.length }} 台</span>
                </div>
                <div class="rack-devices">
                  <div
                    v-for="dev in rackGroup.devices"
                    :key="dev.id"
                    class="device-card"
                    :class="{ checked: tempDeviceIds.has(dev.id) }"
                    @click="toggleSelectorDevice(dev.id, !tempDeviceIds.has(dev.id))"
                  >
                    <el-checkbox :model-value="tempDeviceIds.has(dev.id)" @click.stop @change="(val: boolean) => toggleSelectorDevice(dev.id, val)" />
                    <div class="device-card-visual" :class="'type-' + dev.type">
                      <component :is="getDeviceIcon(dev.type)" />
                    </div>
                    <div class="device-card-info">
                      <div class="device-card-name">{{ dev.name }}</div>
                      <div class="device-card-meta">
                        <span class="device-type-tag" :class="'tag-' + dev.type">{{ deviceTypeLabel(dev.type) }}</span>
                        <span class="device-card-ip">{{ dev.ip_address || '-' }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </template>
            <el-empty v-if="selectorDevices.length === 0 && !selectorDevicesLoading" description="暂无设备" :image-size="60" />
          </div>
        </div>
        <div class="device-selector-main" v-else>
          <el-empty description="请选择机房查看设备" :image-size="60" />
        </div>
      </div>
      <template #footer>
        <span style="margin-right: var(--dcn-space-3); color: var(--dcn-text-secondary);">已选择 {{ tempDeviceIds.size }} 台设备</span>
        <el-button @click="deviceSelectorVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmDeviceSelector">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { userAPI, roleAPI, groupAPI, roomAPI, deviceAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type { User, Role, UserGroup, PermissionGroup } from '@/types'

const authStore = useAuthStore()

function formatTime(val: string | null): string {
  if (!val) return '-'
  return val.replace('T', ' ').substring(0, 19)
}

const props = defineProps<{
  initialTab?: string
}>()

const emit = defineEmits<{
  'tab-change': [tab: string]
}>()

const activeTab = ref(props.initialTab || 'users')

watch(activeTab, (val) => {
  emit('tab-change', val)
})

// ---- Users ----
const users = ref<User[]>([])
const usersLoading = ref(false)

async function loadUsers() {
  usersLoading.value = true
  try {
    const res = await userAPI.list()
    users.value = res.data
  } catch {
    ElMessage.error('加载用户列表失败')
  } finally {
    usersLoading.value = false
  }
}

async function toggleUserActive(row: User, val: boolean) {
  try {
    await userAPI.toggleActive(row.id, val)
    row.is_active = val ? 1 : 0
    ElMessage.success(val ? '已启用' : '已禁用')
  } catch {
    ElMessage.error('状态切换失败')
  }
}

async function deleteUser(row: User) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」？`, '删除确认', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await userAPI.delete(row.id)
    ElMessage.success('用户已删除')
    await loadUsers()
  } catch { /* cancelled or error */ }
}

const userFormVisible = ref(false)
const userFormIsEdit = ref(false)
const userFormEditingId = ref<number | null>(null)
const userFormSubmitting = ref(false)
const userFormRef = ref<FormInstance>()

interface UserFormData {
  username: string
  password: string
  display_name: string
  role_id: number | null
  group_id: number | null
  is_active: boolean
}

const defaultUserForm = (): UserFormData => ({
  username: '',
  password: '',
  display_name: '',
  role_id: null,
  group_id: null,
  is_active: true,
})

const userFormData = reactive<UserFormData>(defaultUserForm())

const userFormRules = reactive<FormRules<UserFormData>>({
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    {
      validator: (_rule, value, callback) => {
        if (!userFormIsEdit.value && !value) {
          callback(new Error('请输入密码'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
})

function openUserForm(row: User | null) {
  userFormIsEdit.value = !!row
  userFormEditingId.value = row ? row.id : null
  if (row) {
    Object.assign(userFormData, {
      username: row.username,
      password: '',
      display_name: row.display_name || '',
      role_id: row.role_id,
      group_id: row.group_id,
      is_active: !!row.is_active,
    })
  } else {
    Object.assign(userFormData, defaultUserForm())
  }
  userFormRef.value?.clearValidate()
  userFormVisible.value = true
}

function resetUserForm() {
  userFormRef.value?.resetFields()
  Object.assign(userFormData, defaultUserForm())
}

async function submitUserForm() {
  const valid = await userFormRef.value?.validate().catch(() => false)
  if (!valid) return

  userFormSubmitting.value = true
  try {
    const payload: Record<string, any> = {
      username: userFormData.username,
      display_name: userFormData.display_name || undefined,
      role_id: userFormData.role_id,
      group_id: userFormData.group_id,
      is_active: userFormData.is_active ? 1 : 0,
    }
    if (userFormData.password) {
      payload.password = userFormData.password
    }

    if (userFormIsEdit.value && userFormEditingId.value) {
      await userAPI.update(userFormEditingId.value, payload)
      ElMessage.success('用户更新成功')
    } else {
      await userAPI.create(payload as any)
      ElMessage.success('用户创建成功')
    }
    userFormVisible.value = false
    await loadUsers()
  } catch {
    ElMessage.error(userFormIsEdit.value ? '更新用户失败' : '创建用户失败')
  } finally {
    userFormSubmitting.value = false
  }
}

// ---- Roles ----
const roles = ref<Role[]>([])
const rolesLoading = ref(false)

async function loadRoles() {
  rolesLoading.value = true
  try {
    const res = await roleAPI.list()
    roles.value = res.data
  } catch {
    ElMessage.error('加载角色列表失败')
  } finally {
    rolesLoading.value = false
  }
}

const permissionGroups = ref<PermissionGroup[]>([])
const permissionMap = ref<Record<string, string>>({})

async function loadPermissions() {
  try {
    const res = await roleAPI.permissions()
    permissionGroups.value = res.data.groups || []
    const map: Record<string, string> = {}
    for (const p of res.data.permissions || []) {
      map[p.key] = p.label
    }
    permissionMap.value = map
  } catch { /* ignore */ }
}

function permissionLabel(key: string): string {
  return permissionMap.value[key] || key
}

const allPermissionKeys = computed(() => {
  const keys: string[] = []
  for (const g of permissionGroups.value) {
    keys.push(...g.permissions)
  }
  return keys
})

async function deleteRole(row: Role) {
  try {
    await ElMessageBox.confirm(`确定删除角色「${row.name}」？`, '删除确认', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await roleAPI.delete(row.id)
    ElMessage.success('角色已删除')
    await loadRoles()
  } catch { /* cancelled or error */ }
}

const roleFormVisible = ref(false)
const roleFormIsEdit = ref(false)
const roleFormEditingId = ref<number | null>(null)
const roleFormSubmitting = ref(false)
const roleFormRef = ref<FormInstance>()

interface RoleFormData {
  name: string
  description: string
  permissions: string[]
  device_scope: string
  device_ids: number[]
}

const defaultRoleForm = (): RoleFormData => ({
  name: '',
  description: '',
  permissions: [],
  device_scope: 'all',
  device_ids: [],
})

const roleFormData = reactive<RoleFormData>(defaultRoleForm())

const roleFormRules = reactive<FormRules<RoleFormData>>({
  name: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
})

// ---- Device Selector State ----
const deviceSelectorVisible = ref(false)
const selectorRooms = ref<{ id: number; name: string }[]>([])
const selectorDevices = ref<{ id: number; name: string; ip_address: string | null; type: string; rack_id: number; rack_name: string }[]>([])
const selectorDevicesLoading = ref(false)
const selectedSelectorRoomId = ref<number | null>(null)
const tempDeviceIds = ref<Set<number>>(new Set())

const selectorGroupedByRack = computed(() => {
  const map = new Map<number, { rack_id: number; rack_name: string; devices: typeof selectorDevices.value }>()
  for (const d of selectorDevices.value) {
    if (!map.has(d.rack_id)) {
      map.set(d.rack_id, { rack_id: d.rack_id, rack_name: d.rack_name, devices: [] })
    }
    map.get(d.rack_id)!.devices.push(d)
  }
  return [...map.values()]
})

const selectorAllChecked = computed(() => {
  if (selectorDevices.value.length === 0) return false
  return selectorDevices.value.every(d => tempDeviceIds.value.has(d.id))
})
const selectorIndeterminate = computed(() => {
  const checked = selectorDevices.value.filter(d => tempDeviceIds.value.has(d.id)).length
  return checked > 0 && checked < selectorDevices.value.length
})

function deviceTypeLabel(type: string): string {
  const map: Record<string, string> = {
    server: '服务器', switch: '交换机', router: '路由器',
    firewall: '防火墙', host: '主机',
  }
  return map[type.toLowerCase()] ?? type
}

function getDeviceIcon(type: string) {
  const t = type.toLowerCase()
  const icons: Record<string, () => ReturnType<typeof h>> = {
    server: () => h('svg', { viewBox: '0 0 40 40', width: 36, height: 36 }, [
      h('rect', { x: 6, y: 2, width: 28, height: 36, rx: 2, fill: '#3f3f46', stroke: '#52525b', 'stroke-width': 1 }),
      h('rect', { x: 10, y: 6, width: 20, height: 6, rx: 1, fill: '#52525b' }),
      h('circle', { cx: 13, cy: 9, r: 1.5, fill: '#10b981' }),
      h('rect', { x: 16, y: 7.5, width: 12, height: 3, rx: 0.5, fill: '#71717a' }),
      h('rect', { x: 10, y: 15, width: 20, height: 6, rx: 1, fill: '#52525b' }),
      h('circle', { cx: 13, cy: 18, r: 1.5, fill: '#8b5cf6' }),
      h('rect', { x: 16, y: 16.5, width: 12, height: 3, rx: 0.5, fill: '#71717a' }),
      h('rect', { x: 10, y: 24, width: 20, height: 6, rx: 1, fill: '#52525b' }),
      h('circle', { cx: 13, cy: 27, r: 1.5, fill: '#8b5cf6' }),
      h('rect', { x: 16, y: 25.5, width: 12, height: 3, rx: 0.5, fill: '#71717a' }),
    ]),
    switch: () => h('svg', { viewBox: '0 0 40 40', width: 36, height: 36 }, [
      h('rect', { x: 2, y: 8, width: 36, height: 24, rx: 2, fill: '#3f3f46', stroke: '#52525b', 'stroke-width': 1 }),
      h('rect', { x: 6, y: 13, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 11, y: 13, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 16, y: 13, width: 3, height: 5, rx: 0.5, fill: '#10b981' }),
      h('rect', { x: 21, y: 13, width: 3, height: 5, rx: 0.5, fill: '#10b981' }),
      h('rect', { x: 26, y: 13, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 31, y: 13, width: 3, height: 5, rx: 0.5, fill: '#f59e0b' }),
      h('rect', { x: 6, y: 22, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 11, y: 22, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 16, y: 22, width: 3, height: 5, rx: 0.5, fill: '#10b981' }),
      h('rect', { x: 21, y: 22, width: 3, height: 5, rx: 0.5, fill: '#10b981' }),
      h('rect', { x: 26, y: 22, width: 3, height: 5, rx: 0.5, fill: '#8b5cf6' }),
      h('rect', { x: 31, y: 22, width: 3, height: 5, rx: 0.5, fill: '#f59e0b' }),
    ]),
    router: () => h('svg', { viewBox: '0 0 40 40', width: 36, height: 36 }, [
      h('rect', { x: 4, y: 14, width: 32, height: 20, rx: 3, fill: '#3f3f46', stroke: '#52525b', 'stroke-width': 1 }),
      h('path', { d: 'M14 14 L14 8 Q14 4 18 4 L22 4 Q26 4 26 8 L26 14', fill: 'none', stroke: '#71717a', 'stroke-width': 1.5 }),
      h('circle', { cx: 20, cy: 4, r: 2, fill: '#71717a' }),
      h('circle', { cx: 12, cy: 24, r: 2, fill: '#10b981' }),
      h('circle', { cx: 20, cy: 24, r: 2, fill: '#8b5cf6' }),
      h('circle', { cx: 28, cy: 24, r: 2, fill: '#8b5cf6' }),
      h('rect', { x: 8, y: 18, width: 24, height: 2, rx: 0.5, fill: '#52525b' }),
    ]),
    firewall: () => h('svg', { viewBox: '0 0 40 40', width: 36, height: 36 }, [
      h('path', { d: 'M20 2 L36 10 L36 22 Q36 34 20 38 Q4 34 4 22 L4 10 Z', fill: '#422006', stroke: '#f59e0b', 'stroke-width': 1.5 }),
      h('path', { d: 'M20 8 L28 12 L28 20 Q28 28 20 31 Q12 28 12 20 L12 12 Z', fill: '#451a03', stroke: '#f59e0b', 'stroke-width': 0.8 }),
      h('rect', { x: 15, y: 16, width: 10, height: 3, rx: 0.5, fill: '#f59e0b' }),
      h('rect', { x: 15, y: 21, width: 10, height: 3, rx: 0.5, fill: '#f59e0b' }),
      h('circle', { cx: 20, cy: 13, r: 1.5, fill: '#ef4444' }),
    ]),
    host: () => h('svg', { viewBox: '0 0 40 40', width: 36, height: 36 }, [
      h('rect', { x: 6, y: 4, width: 28, height: 24, rx: 2, fill: '#3f3f46', stroke: '#52525b', 'stroke-width': 1 }),
      h('rect', { x: 9, y: 7, width: 22, height: 15, rx: 1, fill: '#27272a' }),
      h('rect', { x: 11, y: 9, width: 18, height: 11, rx: 0.5, fill: '#18181b' }),
      h('circle', { cx: 20, cy: 25, r: 1.5, fill: '#8b5cf6' }),
      h('rect', { x: 14, y: 28, width: 12, height: 2, rx: 0.5, fill: '#52525b' }),
      h('rect', { x: 10, y: 30, width: 20, height: 2, rx: 1, fill: '#71717a' }),
    ]),
  }
  return icons[t] || icons.host
}

function onSelectorAllChange(val: boolean) {
  for (const d of selectorDevices.value) {
    if (val) tempDeviceIds.value.add(d.id)
    else tempDeviceIds.value.delete(d.id)
  }
}

function toggleSelectorDevice(id: number, val: boolean) {
  if (val) tempDeviceIds.value.add(id)
  else tempDeviceIds.value.delete(id)
}

async function selectSelectorRoom(roomId: number) {
  selectedSelectorRoomId.value = roomId
  selectorDevicesLoading.value = true
  selectorDevices.value = []
  try {
    const res = await deviceAPI.listByRoom(roomId)
    selectorDevices.value = res.data.map((d: any) => ({
      id: d.id, name: d.name, ip_address: d.ip_address, type: d.type,
      rack_id: d.rack_id, rack_name: d.rack_name,
    }))
  } catch { selectorDevices.value = [] }
  finally { selectorDevicesLoading.value = false }
}

async function openDeviceSelector() {
  tempDeviceIds.value = new Set(roleFormData.device_ids)
  selectedSelectorRoomId.value = null
  selectorDevices.value = []
  deviceSelectorVisible.value = true
  try {
    const res = await roomAPI.list()
    selectorRooms.value = res.data.map((r: any) => ({ id: r.id, name: r.name }))
  } catch { selectorRooms.value = [] }
}

function confirmDeviceSelector() {
  roleFormData.device_ids = [...tempDeviceIds.value]
  deviceSelectorVisible.value = false
}

function onDeviceScopeChange(val: string) {
  if (val === 'selected') {
    openDeviceSelector()
  } else {
    roleFormData.device_ids = []
  }
}

function openRoleForm(row: Role | null) {
  roleFormIsEdit.value = !!row
  roleFormEditingId.value = row ? row.id : null
  if (row) {
    Object.assign(roleFormData, {
      name: row.name,
      description: row.description || '',
      permissions: [...(row.permissions || [])],
      device_scope: row.device_scope || 'all',
      device_ids: [...(row.device_ids || [])],
    })
  } else {
    Object.assign(roleFormData, defaultRoleForm())
  }
  roleFormRef.value?.clearValidate()
  roleFormVisible.value = true
}

function resetRoleForm() {
  roleFormRef.value?.resetFields()
  Object.assign(roleFormData, defaultRoleForm())
}

async function submitRoleForm() {
  const valid = await roleFormRef.value?.validate().catch(() => false)
  if (!valid) return

  roleFormSubmitting.value = true
  try {
    const payload: Record<string, any> = {
      name: roleFormData.name,
      description: roleFormData.description || undefined,
      permissions: roleFormData.permissions,
      device_scope: roleFormData.device_scope,
      device_ids: roleFormData.device_scope === 'selected' ? roleFormData.device_ids : [],
    }

    if (roleFormIsEdit.value && roleFormEditingId.value) {
      await roleAPI.update(roleFormEditingId.value, payload)
      ElMessage.success('角色更新成功')
    } else {
      await roleAPI.create(payload)
      ElMessage.success('角色创建成功')
    }
    roleFormVisible.value = false
    await loadRoles()
  } catch {
    ElMessage.error(roleFormIsEdit.value ? '更新角色失败' : '创建角色失败')
  } finally {
    roleFormSubmitting.value = false
  }
}

// ---- Groups ----
const groups = ref<UserGroup[]>([])
const groupsLoading = ref(false)

async function loadGroups() {
  groupsLoading.value = true
  try {
    const res = await groupAPI.list()
    groups.value = res.data
  } catch {
    ElMessage.error('加载分组列表失败')
  } finally {
    groupsLoading.value = false
  }
}

async function deleteGroup(row: UserGroup) {
  try {
    await ElMessageBox.confirm(`确定删除分组「${row.name}」？`, '删除确认', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await groupAPI.delete(row.id)
    ElMessage.success('分组已删除')
    await loadGroups()
  } catch { /* cancelled or error */ }
}

const groupFormVisible = ref(false)
const groupFormIsEdit = ref(false)
const groupFormEditingId = ref<number | null>(null)
const groupFormSubmitting = ref(false)
const groupFormRef = ref<FormInstance>()

interface GroupFormData {
  name: string
  description: string
}

const defaultGroupForm = (): GroupFormData => ({ name: '', description: '' })
const groupFormData = reactive<GroupFormData>(defaultGroupForm())

const groupFormRules = reactive<FormRules<GroupFormData>>({
  name: [{ required: true, message: '请输入分组名称', trigger: 'blur' }],
})

function openGroupForm(row: UserGroup | null) {
  groupFormIsEdit.value = !!row
  groupFormEditingId.value = row ? row.id : null
  if (row) {
    Object.assign(groupFormData, {
      name: row.name,
      description: row.description || '',
    })
  } else {
    Object.assign(groupFormData, defaultGroupForm())
  }
  groupFormRef.value?.clearValidate()
  groupFormVisible.value = true
}

function resetGroupForm() {
  groupFormRef.value?.resetFields()
  Object.assign(groupFormData, defaultGroupForm())
}

async function submitGroupForm() {
  const valid = await groupFormRef.value?.validate().catch(() => false)
  if (!valid) return

  groupFormSubmitting.value = true
  try {
    const payload = {
      name: groupFormData.name,
      description: groupFormData.description || undefined,
    }

    if (groupFormIsEdit.value && groupFormEditingId.value) {
      await groupAPI.update(groupFormEditingId.value, payload)
      ElMessage.success('分组更新成功')
    } else {
      await groupAPI.create(payload)
      ElMessage.success('分组创建成功')
    }
    groupFormVisible.value = false
    await loadGroups()
  } catch {
    ElMessage.error(groupFormIsEdit.value ? '更新分组失败' : '创建分组失败')
  } finally {
    groupFormSubmitting.value = false
  }
}

onMounted(() => {
  Promise.all([loadUsers(), loadRoles(), loadGroups(), loadPermissions()])
})
</script>

<style scoped>
.device-selector {
  display: flex;
  min-height: 360px;
  border: 1px solid var(--dcn-border-strong);
  border-radius: var(--dcn-radius-lg);
  overflow: hidden;
}
.device-selector-sidebar {
  width: 160px;
  border-right: 1px solid var(--dcn-border-strong);
  overflow-y: auto;
  flex-shrink: 0;
  background: var(--dcn-bg-elevated);
}
.device-selector-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.device-selector-section-title {
  padding: 10px 14px;
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: var(--dcn-text-regular);
  background: var(--dcn-bg-section);
  border-bottom: 1px solid var(--dcn-border-strong);
}
.device-selector-item {
  padding: 9px 14px;
  cursor: pointer;
  font-size: var(--dcn-text-base);
  border-bottom: 1px solid var(--dcn-border-light);
  transition: all var(--dcn-transition-fast);
}
.device-selector-item:hover {
  background: var(--dcn-primary-bg);
}
.device-selector-item.active {
  background: var(--dcn-primary);
  color: var(--dcn-text-white);
}

.device-selector-header {
  padding: 10px var(--dcn-space-4);
  border-bottom: 1px solid var(--dcn-border);
  background: var(--dcn-bg-card);
  flex-shrink: 0;
}
.device-selector-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-3) var(--dcn-space-4);
}

.rack-group {
  margin-bottom: var(--dcn-space-4);
}
.rack-group:last-child {
  margin-bottom: 0;
}
.rack-group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 0 8px;
  font-size: var(--dcn-text-base);
  font-weight: 600;
  color: var(--dcn-text-primary);
  border-bottom: 1px solid var(--dcn-bg-page);
  margin-bottom: var(--dcn-space-2);
}
.rack-icon {
  color: var(--dcn-text-secondary);
}
.rack-group-name {
  flex: 1;
}
.rack-group-count {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  font-weight: 400;
}

.rack-devices {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--dcn-space-2);
}
.device-card {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) 10px;
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
}
.device-card:hover {
  border-color: var(--dcn-border-strong);
  box-shadow: var(--dcn-shadow-sm);
}
.device-card.checked {
  border-color: var(--dcn-primary);
  background: var(--dcn-primary-bg-deep);
}
.device-card-visual {
  width: 36px;
  height: 36px;
  border-radius: var(--dcn-radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
}
.device-card-visual.type-server { background: var(--dcn-device-host-bg); }
.device-card-visual.type-switch { background: var(--dcn-device-server-bg); }
.device-card-visual.type-router { background: var(--dcn-device-router-bg); }
.device-card-visual.type-firewall { background: var(--dcn-warning-light); }
.device-card-visual.type-host { background: var(--dcn-device-host-bg); }
.device-card-info {
  flex: 1;
  min-width: 0;
}
.device-card-name {
  font-size: var(--dcn-text-base);
  font-weight: 500;
  color: var(--dcn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.device-card-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
}
.device-type-tag {
  font-size: var(--dcn-text-xs);
  padding: 0 var(--dcn-space-1);
  border-radius: 3px;
  line-height: 18px;
  white-space: nowrap;
}
.device-type-tag.tag-server { background: var(--dcn-device-host-bg); color: var(--dcn-device-host); }
.device-type-tag.tag-switch { background: var(--dcn-device-switch-bg); color: var(--dcn-device-switch); }
.device-type-tag.tag-router { background: var(--dcn-device-router-bg); color: var(--dcn-device-router); }
.device-type-tag.tag-firewall { background: var(--dcn-device-firewall-bg); color: var(--dcn-device-firewall); }
.device-type-tag.tag-host { background: var(--dcn-device-host-bg); color: var(--dcn-device-host); }
.device-card-ip {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
