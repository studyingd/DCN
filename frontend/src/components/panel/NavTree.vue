<template>
  <div class="nav-tree" :class="{ collapsed }">
    <div class="nav-tree-header">
      <span v-if="!collapsed" class="nav-tree-title">导航</span>
      <button v-else class="collapse-icon-static" type="button" aria-label="展开导航" @click="$emit('toggle-collapse')">
        <el-icon><Expand /></el-icon>
      </button>
    </div>
    <div class="nav-menu">
      <button
        v-for="item in menuItems"
        :key="item.key"
        type="button"
        class="nav-menu-item"
        :class="{ active: activePanel === item.key }"
        :aria-current="activePanel === item.key ? 'page' : undefined"
        :title="collapsed ? item.label : undefined"
        @click="$emit('nav-panel', item.key)"
      >
        <el-icon><component :is="item.icon" /></el-icon
        ><span class="nav-menu-label" :class="{ hidden: collapsed }">{{ item.label }}</span>
      </button>
    </div>
    <div class="nav-tree-footer">
      <button
        class="collapse-btn"
        type="button"
        :aria-label="collapsed ? '展开导航' : '收起导航'"
        :title="collapsed ? '展开导航' : undefined"
        @click="$emit('toggle-collapse')"
      >
        <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon
        ><span class="collapse-text" :class="{ hidden: collapsed }">收起导航</span>
      </button>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, type Component } from 'vue'
import {
  HomeFilled,
  OfficeBuilding,
  MagicStick,
  Odometer,
  Setting,
  Box,
  Briefcase,
  Platform,
  Expand,
  Fold,
  Bell,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
defineProps<{ activePanel: string; collapsed: boolean }>()
defineEmits<{ 'nav-panel': [panel: string]; 'toggle-collapse': [] }>()
const authStore = useAuthStore()
const menuItems = computed<{ key: string; label: string; icon: Component; visible: boolean }[]>(() =>
  [
    { key: 'home', label: '系统首页', icon: HomeFilled, visible: true },
    { key: 'rooms', label: '机房管理', icon: OfficeBuilding, visible: true },
    { key: 'metrics', label: '服务器监控', icon: Odometer, visible: authStore.hasPermission('device:view') },
    { key: 'pve', label: '虚拟化管理', icon: Platform, visible: authStore.hasPermission('device:view') },
    { key: 'containers', label: '容器管理', icon: Box, visible: authStore.hasPermission('device:view') },
    { key: 'business', label: '业务监控', icon: Briefcase, visible: authStore.hasPermission('device:view') },
    { key: 'alerts', label: '告警中心', icon: Bell, visible: authStore.hasPermission('automation:manage') },
    {
      key: 'automation',
      label: '自动化运维',
      icon: MagicStick,
      visible:
        authStore.hasPermission('automation:manage') ||
        authStore.hasPermission('automation:manage') ||
        authStore.hasPermission('device:remote'),
    },
    {
      key: 'system',
      label: '系统设置',
      icon: Setting,
      visible: authStore.hasPermission('user:manage') || authStore.hasPermission('settings:manage'),
    },
  ].filter((x) => x.visible),
)
</script>
<style scoped>
.nav-tree {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 8px;
  background: var(--dcn-bg-section);
  overflow: hidden;
}
.nav-tree-header {
  padding: 8px 10px 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
}
.nav-tree-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--dcn-text-secondary);
  text-transform: uppercase;
  letter-spacing: 1.5px;
  width: 100%;
}
.collapse-icon-static {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  border: 0;
  background: transparent;
  /* 收起态唯一的「展开」入口,用主色而非占位符灰,避免在深色侧栏里隐形 */
  color: var(--dcn-primary-light);
  cursor: pointer;
  border-radius: var(--dcn-radius-md);
  transition: all var(--dcn-transition-fast);
}
.collapse-icon-static:hover {
  background: var(--dcn-sidebar-hover-bg);
}
.nav-menu {
  padding: 0;
  flex: 1;
  overflow-y: auto;
}
.nav-menu-item {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  min-height: 40px;
  padding: 8px 10px;
  border-radius: var(--dcn-radius-md);
  font-size: var(--dcn-text-md);
  color: var(--dcn-text-regular);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  width: 100%;
  border: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  margin-bottom: 3px;
  white-space: nowrap;
  overflow: hidden;
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
/* 文字标签常驻 DOM,用 opacity + max-width 与侧栏宽度动画(150ms)同步,
   收起时先淡出再收宽度,展开时宽度到位后淡入——避免 v-if 瞬移的突兀感。 */
.nav-menu-label,
.collapse-text {
  display: inline-block;
  max-width: 160px;
  opacity: 1;
  overflow: hidden;
  white-space: nowrap;
  /* 与侧栏宽度动画(--dcn-transition-normal, 220ms)同节奏 */
  transition:
    opacity var(--dcn-transition-normal),
    max-width var(--dcn-transition-normal);
}
.nav-menu-label.hidden,
.collapse-text.hidden {
  max-width: 0;
  opacity: 0;
}
.nav-menu-item.active .el-icon {
  color: var(--dcn-primary-light);
}
.collapsed .nav-menu-item {
  justify-content: center;
  padding: 7px 0;
}
.nav-tree-footer {
  padding: 8px 0 0;
  border-top: 1px solid var(--dcn-border);
}
.collapse-btn {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  min-height: 36px;
  padding: 6px 10px;
  border-radius: var(--dcn-radius-md);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  cursor: pointer;
  width: 100%;
  border: 0;
  background: transparent;
}
.collapsed .collapse-btn {
  justify-content: center;
  padding: 6px 0;
}
.collapse-btn:hover {
  background: var(--dcn-sidebar-hover-bg);
  color: var(--dcn-text-primary);
}
</style>
