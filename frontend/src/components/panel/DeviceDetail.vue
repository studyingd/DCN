<template>
  <div class="device-detail">
    <template v-if="deviceStore.currentDevice">
      <div class="detail-header">
        <el-icon :size="20" class="device-header-icon"><Monitor /></el-icon>
        <h3 class="detail-title">{{ deviceStore.currentDevice.name }}</h3>
        <el-tag
          :type="statusTagType"
          size="small"
          effect="dark"
          style="margin-left: auto;"
        >
          {{ statusLabel }}
        </el-tag>
      </div>

      <el-descriptions :column="1" border size="small" class="detail-descriptions">
        <el-descriptions-item label="设备类型">
          {{ deviceStore.currentDevice.type || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="IP 地址">
          <code>{{ deviceStore.currentDevice.ip_address || '-' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="访问网址">
          <a
            v-if="deviceStore.currentDevice.web_url"
            :href="deviceStore.currentDevice.web_url"
            target="_blank"
            rel="noopener noreferrer"
            class="web-url-link"
          >
            {{ deviceStore.currentDevice.web_url }}
            <el-icon style="vertical-align: middle; margin-left: 2px;"><TopRight /></el-icon>
          </a>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="负责人">
          {{ deviceStore.currentDevice.owner || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="用途">
          {{ deviceStore.currentDevice.purpose || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="操作系统">
          <span v-if="osIsLinux || osIsWindows">{{ deviceStore.currentDevice.os_system }}</span>
          <span v-else>{{ deviceStore.currentDevice.os_system || '-' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="位置 (U)">
          {{ deviceStore.currentDevice.position_u != null ? `${deviceStore.currentDevice.position_u} U` : '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="尺寸 (U)">
          {{ deviceStore.currentDevice.size_u != null ? `${deviceStore.currentDevice.size_u} U` : '-' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="showSshPort" label="SSH 端口">
          {{ deviceStore.currentDevice.ssh_port || '-' }}
        </el-descriptions-item>
        <el-descriptions-item v-if="showRdpPort" label="RDP 端口">
          {{ deviceStore.currentDevice.rdp_port || '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="所属机柜 ID">
          {{ deviceStore.currentDevice.rack_id }}
        </el-descriptions-item>
        <el-descriptions-item label="创建时间">
          {{ formatDate(deviceStore.currentDevice.created_at) }}
        </el-descriptions-item>
      </el-descriptions>

      <div class="detail-actions">
        <el-button v-if="osIsLinux && authStore.hasPermission('device:remote')" type="primary" style="flex: 1;" @click="$emit('connect-ssh')">
          <el-icon><Link /></el-icon>
          SSH 连接
        </el-button>
        <el-button v-if="osIsWindows && authStore.hasPermission('device:remote')" type="success" style="flex: 1;" @click="$emit('connect-rdp')">
          <el-icon><Monitor /></el-icon>
          RDP 连接
        </el-button>
      </div>
    </template>

    <template v-else>
      <div class="detail-empty">
        <el-icon :size="48" style="color: var(--dcn-text-placeholder);"><Monitor /></el-icon>
        <p class="empty-text">请选择设备查看详情</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Monitor, Link, TopRight } from '@element-plus/icons-vue'
import { useDeviceStore } from '@/stores/device'
import { useAuthStore } from '@/stores/auth'

defineEmits<{
  'connect-ssh': []
  'connect-rdp': []
}>()

const deviceStore = useDeviceStore()
const authStore = useAuthStore()

const statusTagType = computed(() => {
  const status = deviceStore.currentDevice?.status?.toLowerCase()
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  if (status === 'maintenance') return 'warning'
  return 'info'
})

const device = computed(() => deviceStore.currentDevice)

// OS detection — use substring match to handle specific distro names
// e.g. "Rocky Linux 10.0", "Windows Server 2022", "Ubuntu 22.04"
const osIsLinux = computed(() => {
  const os = device.value?.os_system?.toLowerCase() || ''
  return os.includes('linux') && !os.includes('windows')
})
const osIsWindows = computed(() => {
  const os = device.value?.os_system?.toLowerCase() || ''
  return os.includes('windows')
})
const isNetworkDevice = computed(() => {
  const type = device.value?.type?.toLowerCase() || ''
  return ['switch', 'router', 'firewall'].includes(type)
})

// Port visibility: Windows → RDP only; Linux / Network → SSH only; unknown → both
const showSshPort = computed(() => !osIsWindows.value)
const showRdpPort = computed(() => osIsWindows.value || (!osIsLinux.value && !isNetworkDevice.value))

const statusLabel = computed(() => {
  const status = deviceStore.currentDevice?.status?.toLowerCase()
  if (status === 'online') return '在线'
  if (status === 'offline') return '离线'
  if (status === 'maintenance') return '维护中'
  return deviceStore.currentDevice?.status || '未知'
})

function formatDate(dateStr: string): string {
  if (!dateStr) return '-'
  try {
    return new Date(dateStr).toLocaleString('zh-CN')
  } catch {
    return dateStr
  }
}
</script>

<style scoped>
.device-detail {
  padding: var(--dcn-space-4);
  height: 100%;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-4);
  padding-bottom: var(--dcn-space-3);
  border-bottom: 1px solid var(--dcn-border);
}

.device-header-icon {
  color: var(--dcn-warning);
}

.detail-title {
  margin: 0;
  font-size: var(--dcn-text-lg);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.detail-descriptions {
  flex: 1;
  overflow-y: auto;
}

.detail-descriptions :deep(.el-descriptions__label) {
  width: 100px;
  font-weight: 500;
  color: var(--dcn-text-regular);
}

.detail-descriptions code {
  background: var(--dcn-bg-section);
  padding: 2px 6px;
  border-radius: var(--dcn-radius-sm);
  font-size: var(--dcn-text-base);
  color: var(--dcn-primary);
}

.web-url-link {
  color: var(--dcn-primary);
  text-decoration: none;
  word-break: break-all;
  font-size: var(--dcn-text-base);
}

.web-url-link:hover {
  color: var(--dcn-primary-light);
  text-decoration: underline;
}

.detail-actions {
  display: flex;
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-4);
  padding-top: var(--dcn-space-3);
  border-top: 1px solid var(--dcn-border);
}

.detail-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: var(--dcn-space-3);
}

.empty-text {
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-md);
  margin: 0;
}
</style>
