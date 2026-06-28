<template>
  <div
    v-if="visible"
    class="context-menu"
    :style="menuStyle"
    @click.stop
  >
    <div
      v-for="item in menuItems"
      :key="item.action"
      class="context-menu__item"
      @click="handleAction(item.action)"
    >
      <el-icon v-if="item.icon" class="context-menu__icon"><component :is="item.icon" /></el-icon>
      <span>{{ item.label }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  Edit,
  Delete,
  Connection,
  Link,
  Monitor,
  Plus,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

interface MenuTarget {
  type: string
  id: number
  data: Record<string, unknown>
}

const props = withDefaults(
  defineProps<{
    visible: boolean
    position: { x: number; y: number }
    target?: MenuTarget | null
  }>(),
  {
    target: null,
  }
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'action', payload: { action: string; target: MenuTarget | null }): void
}>()

const authStore = useAuthStore()

interface MenuItem {
  label: string
  action: string
  icon?: unknown
  permission?: string
}

const menuItems = computed<MenuItem[]>(() => {
  if (!props.target) {
    return [{ label: '新建机房', action: 'create-room', icon: Plus, permission: 'device:manage' }]
  }

  const all: MenuItem[] = []
  switch (props.target.type) {
    case 'device':
      all.push(
        { label: '编辑设备', action: 'edit-device', icon: Edit, permission: 'device:manage' },
        { label: '删除设备', action: 'delete-device', icon: Delete, permission: 'device:manage' },
        { label: '查看连接', action: 'view-connections', icon: Connection },
        { label: 'SSH 连接', action: 'ssh-connect', icon: Link, permission: 'device:remote' },
        { label: 'RDP 连接', action: 'rdp-connect', icon: Monitor, permission: 'device:remote' },
      )
      break
    case 'rack':
      all.push(
        { label: '编辑机柜', action: 'edit-rack', icon: Edit, permission: 'device:manage' },
        { label: '删除机柜', action: 'delete-rack', icon: Delete, permission: 'device:manage' },
        { label: '新建设备', action: 'create-device', icon: Plus, permission: 'device:manage' },
      )
      break
    case 'room':
      all.push(
        { label: '编辑机房', action: 'edit-room', icon: Edit, permission: 'device:manage' },
        { label: '删除机房', action: 'delete-room', icon: Delete, permission: 'device:manage' },
        { label: '新建机柜', action: 'create-rack', icon: Plus, permission: 'device:manage' },
      )
      break
    default:
      all.push({ label: '新建机房', action: 'create-room', icon: Plus, permission: 'device:manage' })
  }

  return all.filter(item => !item.permission || authStore.hasPermission(item.permission))
})

const menuStyle = computed(() => ({
  left: `${props.position.x}px`,
  top: `${props.position.y}px`,
}))

function handleAction(action: string) {
  emit('action', { action, target: props.target })
  emit('update:visible', false)
}
</script>

<style scoped>
.context-menu {
  position: fixed;
  z-index: 3000;
  background: var(--dcn-bg-card);
  border-radius: var(--dcn-radius-sm);
  box-shadow: var(--dcn-shadow-md);
  border: 1px solid var(--dcn-border-strong);
  min-width: 160px;
  padding: var(--dcn-space-1) 0;
  user-select: none;
}

.context-menu__item {
  display: flex;
  align-items: center;
  padding: var(--dcn-space-2) var(--dcn-space-4);
  font-size: var(--dcn-text-md);
  color: var(--dcn-text-primary);
  cursor: pointer;
  transition: background-color var(--dcn-transition-normal);
}

.context-menu__item:hover {
  background-color: var(--dcn-primary-bg);
  color: var(--dcn-primary);
}

.context-menu__icon {
  margin-right: var(--dcn-space-2);
  font-size: var(--dcn-text-lg);
}
</style>
