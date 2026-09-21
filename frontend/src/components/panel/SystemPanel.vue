<template>
  <div class="inline-panel system-panel">
    <div class="inline-panel-header system-header">
      <div>
        <h2 class="inline-panel-title">系统设置中心</h2>
      </div>
      <el-tag type="success" effect="dark">配置即时生效</el-tag>
    </div>
    <div class="system-tabs">
      <button
        v-for="item in tabs"
        :key="item.key"
        type="button"
        class="system-tab"
        :class="{ active: activeTab === item.key }"
        @click="selectTab(item.key)"
      >
        <el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span
        ><small>{{ item.description }}</small>
      </button>
    </div>
    <div class="system-body">
      <div v-if="activeTab === 'overview'" class="overview-grid">
        <div class="overview-intro">
          <span class="eyebrow">SYSTEM HEALTH</span>
          <h3>设置总览</h3>
          <p>从这里快速检查平台基础配置是否完整，点击上方工作区即可深入管理。</p>
        </div>
        <div class="stat-card">
          <span class="stat-label">用户数量</span><strong>{{ overview.users }}</strong
          ><el-icon><User /></el-icon>
        </div>
        <div class="stat-card">
          <span class="stat-label">角色数量</span><strong>{{ overview.roles }}</strong
          ><el-icon><Lock /></el-icon>
        </div>
        <div class="stat-card">
          <span class="stat-label">Agent 状态</span
          ><strong :class="overview.agent ? 'good' : 'muted'">{{ overview.agent ? '已启用' : '未启用' }}</strong
          ><el-icon><Cpu /></el-icon>
        </div>
        <div class="stat-card">
          <span class="stat-label">启用 Webhook</span><strong>{{ overview.webhooks }}</strong
          ><el-icon><Bell /></el-icon>
        </div>
        <div class="overview-note">
          <el-icon><InfoFilled /></el-icon><span>建议先完成 Agent 连接测试，再配置至少一个 Webhook 告警通道。</span>
        </div>
      </div>
      <KeepAlive v-else
        ><component :is="activeComponent" v-bind="activeBindings" @tab-change="onUsersTabChange"
      /></KeepAlive>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch, type Component } from 'vue'
import { Bell, Cpu, InfoFilled, Lock, Setting, Tools, User } from '@element-plus/icons-vue'
import { userAPI, roleAPI, agentAPI, webhookAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'
import UserManagePanel from './UserManagePanel.vue'
import AgentConfigPanel from './AgentConfigPanel.vue'
import WebhookSettingsPanel from './WebhookSettingsPanel.vue'
const props = withDefaults(defineProps<{ initialUsersTab?: string; tab?: string }>(), {
  initialUsersTab: 'users',
  tab: 'overview',
})
const emit = defineEmits<{ 'users-tab-change': [tab: string] }>()
const authStore = useAuthStore()
const activeTab = ref(['overview', 'users', 'agent', 'webhooks'].includes(props.tab) ? props.tab : 'overview')
const overview = reactive({ users: 0, roles: 0, agent: false, webhooks: 0 })
const tabs = computed(() => {
  const items: { key: string; label: string; description: string; icon: Component }[] = [
    { key: 'overview', label: '设置概览', description: '健康状态', icon: Setting },
  ]
  if (authStore.hasPermission('user:manage'))
    items.push({ key: 'users', label: '用户与权限', description: '账号 / 角色', icon: User })
  if (authStore.hasPermission('settings:manage')) {
    items.push({ key: 'agent', label: 'Agent 配置', description: '模型 / 连接', icon: Tools })
    items.push({ key: 'webhooks', label: 'Webhook 告警', description: '通知 / 事件', icon: Bell })
  }
  return items
})
const activeComponent = computed(
  () =>
    (
      ({ users: UserManagePanel, agent: AgentConfigPanel, webhooks: WebhookSettingsPanel }) as Record<string, Component>
    )[activeTab.value] || UserManagePanel,
)
const activeBindings = computed(() =>
  activeTab.value === 'users' ? { embedded: true, initialTab: props.initialUsersTab } : {},
)
function selectTab(key: string) {
  activeTab.value = key
}
function onUsersTabChange(tab: string) {
  emit('users-tab-change', tab)
}
watch(
  () => props.tab,
  (value) => {
    if (value && tabs.value.some((tab) => tab.key === value)) activeTab.value = value
  },
)
onMounted(async () => {
  if (!tabs.value.some((item) => item.key === activeTab.value)) activeTab.value = 'overview'
  const tasks: Promise<any>[] = []
  if (authStore.hasPermission('user:manage')) {
    tasks.push(
      userAPI
        .list()
        .then((r) => (overview.users = r.data.length))
        .catch(() => {}),
    )
    tasks.push(
      roleAPI
        .list()
        .then((r) => (overview.roles = r.data.length))
        .catch(() => {}),
    )
  }
  if (authStore.hasPermission('settings:manage')) {
    tasks.push(
      agentAPI
        .status()
        .then((r) => (overview.agent = !!(r.data as any).enabled))
        .catch(() => {}),
    )
    tasks.push(
      webhookAPI
        .list()
        .then((r) => (overview.webhooks = r.data.filter((x) => x.enabled).length))
        .catch(() => {}),
    )
  }
  await Promise.all(tasks)
})
</script>
<style scoped>
.system-panel {
  background: var(--dcn-bg-page);
}
.system-header {
  align-items: flex-start;
  padding-bottom: 22px;
}
.system-header p {
  margin: 7px 0 0;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.eyebrow {
  color: var(--dcn-primary-light);
  font: 10px var(--dcn-font-mono);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.system-tabs {
  display: flex;
  gap: 8px;
  padding: 10px var(--dcn-space-5) 16px;
  border-bottom: 1px solid var(--dcn-border);
  overflow-x: auto;
}
.system-tab {
  min-width: 150px;
  padding: 11px 13px;
  display: grid;
  grid-template-columns: 22px 1fr;
  grid-template-rows: auto auto;
  column-gap: 8px;
  text-align: left;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-secondary);
  background: var(--dcn-bg-card);
  cursor: pointer;
  transition: all 0.18s ease;
}
.system-tab:hover {
  border-color: var(--dcn-border-strong);
  color: var(--dcn-text-primary);
  transform: translateY(-1px);
}
.system-tab:focus-visible {
  outline: 2px solid var(--dcn-primary);
  outline-offset: 2px;
}
.system-tab.active {
  border-color: var(--dcn-primary);
  color: var(--dcn-text-primary);
  background: var(--dcn-primary-bg);
}
.system-tab .el-icon {
  grid-row: 1 / span 2;
  align-self: center;
  color: var(--dcn-primary-light);
}
.system-tab span {
  font-size: 13px;
  font-weight: 600;
}
.system-tab small {
  margin-top: 3px;
  color: var(--dcn-text-placeholder);
  font-size: 11px;
}
.system-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-5);
}
.overview-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  max-width: 1100px;
}
.overview-intro {
  grid-column: 1 / -1;
  padding: 4px 0 8px;
}
.overview-intro h3 {
  margin: 6px 0;
  color: var(--dcn-text-primary);
  font-size: 22px;
}
.overview-intro p {
  margin: 0;
  color: var(--dcn-text-secondary);
}
.stat-card {
  position: relative;
  min-height: 112px;
  padding: 17px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-card);
}
.stat-card .el-icon {
  position: absolute;
  right: 16px;
  top: 16px;
  font-size: 20px;
  color: var(--dcn-primary-light);
  opacity: 0.8;
}
.stat-label {
  display: block;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.stat-card strong {
  display: block;
  margin-top: 17px;
  color: var(--dcn-text-primary);
  font-size: 25px;
}
.stat-card strong.good {
  color: var(--dcn-success);
}
.stat-card strong.muted {
  color: var(--dcn-text-placeholder);
  font-size: 18px;
}
.overview-note {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 15px;
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-secondary);
  background: var(--dcn-bg-muted);
  font-size: 13px;
}
.overview-note .el-icon {
  color: var(--dcn-primary-light);
}
@media (max-width: 800px) {
  .overview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .system-body {
    padding: var(--dcn-space-3);
  }
}
@media (max-width: 480px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }
  .overview-intro,
  .overview-note {
    grid-column: auto;
  }
}
</style>
