<template>
  <div class="nav-tree" :class="{ collapsed }">
    <div class="nav-tree-header">
      <span v-if="!collapsed" class="nav-tree-title">导航</span>
      <el-tooltip v-else content="展开导航" placement="right" :show-after="300">
        <el-icon class="collapse-icon-static" @click="$emit('toggle-collapse')"><Expand /></el-icon>
      </el-tooltip>
    </div>

    <div class="nav-menu">
      <el-tooltip v-for="item in menuItems" :key="item.key" :content="item.label" placement="right" :disabled="!collapsed" :show-after="300">
        <div
          v-if="item.visible"
          class="nav-menu-item"
          :class="{ active: activePanel === item.key }"
          @click="$emit('nav-panel', item.key)"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span v-if="!collapsed" class="nav-menu-label">{{ item.label }}</span>
        </div>
      </el-tooltip>
    </div>

    <div class="nav-tree-footer">
      <div class="collapse-btn" @click="$emit('toggle-collapse')">
        <el-icon :size="16">
          <component :is="collapsed ? 'Expand' : 'Fold'" />
        </el-icon>
        <span v-if="!collapsed" class="collapse-text">收起导航</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { HomeFilled, OfficeBuilding, Key, Document, User, Promotion, DataAnalysis, Expand, Fold } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

defineProps<{
  activePanel: string
  collapsed: boolean
}>()

defineEmits<{
  'nav-panel': [panel: string]
  'toggle-collapse': []
}>()

const authStore = useAuthStore()

const menuItems = computed(() => [
  { key: 'home', label: '系统首页', icon: HomeFilled, visible: true },
  { key: 'rooms', label: '机房管理', icon: OfficeBuilding, visible: true },
  { key: 'scripts', label: '脚本管理', icon: Promotion, visible: authStore.hasPermission('script:manage') },
  { key: 'inspection', label: '巡检管理', icon: DataAnalysis, visible: authStore.hasPermission('inspection:manage') },
  { key: 'credentials', label: '凭据管理', icon: Key, visible: authStore.hasPermission('credential:manage') },
  { key: 'audit', label: '审计中心', icon: Document, visible: authStore.hasPermission('audit:manage') },
  { key: 'users', label: '用户管理', icon: User, visible: authStore.hasPermission('user:manage') },
])
</script>

<style scoped>
.nav-tree {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 0;
  background: var(--dcn-bg-card);
  transition: width var(--dcn-transition-normal);
  overflow: hidden;
}

.nav-tree-header {
  padding: var(--dcn-space-3) var(--dcn-space-3) var(--dcn-space-2);
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
}

.nav-tree-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--dcn-text-placeholder);
  text-transform: uppercase;
  letter-spacing: 1.5px;
  width: 100%;
}

.collapse-icon-static {
  font-size: 16px;
  color: var(--dcn-text-placeholder);
  cursor: pointer;
  transition: color var(--dcn-transition-fast);
}
.collapse-icon-static:hover {
  color: var(--dcn-text-primary);
}

.nav-menu {
  padding: var(--dcn-space-1) var(--dcn-space-2);
  flex: 1;
}

.collapsed .nav-menu {
  padding: var(--dcn-space-1) var(--dcn-space-1);
}

.nav-menu-item {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: 7px var(--dcn-space-3);
  border-radius: var(--dcn-radius-md);
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-regular);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  margin-bottom: 1px;
  white-space: nowrap;
  overflow: hidden;
}

.collapsed .nav-menu-item {
  justify-content: center;
  padding: 7px 0;
}

.nav-menu-item:hover {
  background: var(--dcn-sidebar-hover-bg);
  color: var(--dcn-text-primary);
}
.nav-menu-item.active {
  background: var(--dcn-sidebar-active-bg);
  color: var(--dcn-text-primary);
  font-weight: 500;
}
.nav-menu-item .el-icon {
  font-size: 16px;
  flex-shrink: 0;
  color: var(--dcn-text-placeholder);
}
.nav-menu-item.active .el-icon {
  color: var(--dcn-text-secondary);
}

.nav-menu-label {
  transition: opacity var(--dcn-transition-fast);
}

.nav-tree-footer {
  padding: var(--dcn-space-2);
  border-top: 1px solid var(--dcn-border);
}

.collapse-btn {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: 6px var(--dcn-space-3);
  border-radius: var(--dcn-radius-md);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  white-space: nowrap;
  overflow: hidden;
}

.collapsed .collapse-btn {
  justify-content: center;
  padding: 6px 0;
}

.collapse-btn:hover {
  background: var(--dcn-sidebar-hover-bg);
  color: var(--dcn-text-primary);
}

.collapse-text {
  font-size: var(--dcn-text-xs);
}
</style>
