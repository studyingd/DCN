<template>
  <div class="inspection-result-card" :class="{ 'is-expanded': expanded }">
    <!-- Header -->
    <div class="card-header" @click="expanded = !expanded">
      <div class="card-header-left">
        <el-tag :type="statusTagType" size="small" effect="dark" round class="status-tag">
          {{ statusLabel }}
        </el-tag>
        <span class="device-name">{{ result.device_name }}</span>
        <span class="device-ip">{{ result.ip_address || '-' }}</span>
        <el-tag v-if="result.target_type === 'network'" size="small" type="warning" class="vendor-tag">
          {{ result.vendor || '网络设备' }}
        </el-tag>
        <el-tag v-else size="small" :type="result.target_type === 'linux' ? 'success' : 'primary'" class="vendor-tag">
          {{ result.target_type === 'linux' ? 'Linux' : 'Windows' }}
        </el-tag>
      </div>
      <div class="card-header-right">
        <span class="count-summary">
          <span class="count normal">✓ {{ result.normal_count }}</span>
          <span v-if="result.warning_count" class="count warning">⚠ {{ result.warning_count }}</span>
          <span v-if="result.critical_count" class="count critical">✕ {{ result.critical_count }}</span>
          <span v-if="result.error_count" class="count error">? {{ result.error_count }}</span>
        </span>
        <span class="duration">{{ (result.duration_ms / 1000).toFixed(1) }}s</span>
        <el-icon class="expand-icon"><ArrowDown /></el-icon>
      </div>
    </div>

    <!-- Expanded item list -->
    <div v-if="expanded" class="card-body">
      <div class="error-banner" v-if="result.error">
        <el-icon><WarningFilled /></el-icon>
        {{ result.error }}
      </div>
      <div class="items-grid">
        <div
          v-for="item in result.items"
          :key="item.item_type"
          class="item-row"
          :class="'item-' + item.status"
        >
          <div class="item-left">
            <span class="status-dot" :class="'dot-' + item.status" />
            <span class="item-label">{{ getItemLabel(item.item_type) }}</span>
          </div>
          <div class="item-center">
            <span v-if="item.value" class="item-value">{{ item.value }}</span>
            <span v-if="item.unit" class="item-unit">{{ item.unit }}</span>
            <el-tag v-if="item.status !== 'normal'" :type="statusTagTypeOf(item.status)" size="small" effect="plain">
              {{ statusText(item.status) }}
            </el-tag>
          </div>
          <div class="item-right">
            <el-button
              v-if="item.raw_output"
              type="primary"
              link
              size="small"
              @click.stop="showRawOutput(item)"
            >
              原始输出
            </el-button>
            <span v-if="item.error_message" class="item-error" :title="item.error_message">
              {{ item.error_message.slice(0, 40) }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Raw output dialog -->
  <el-dialog v-model="rawDialogVisible" :title="`原始输出 — ${rawDialogTitle}`" width="700px" destroy-on-close>
    <pre class="raw-output-pre">{{ rawDialogContent }}</pre>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ArrowDown, WarningFilled } from '@element-plus/icons-vue'
import type { InspectionDeviceResult, InspectionItemResult } from '@/types/inspection'

const props = defineProps<{
  result: InspectionDeviceResult
  defaultExpanded?: boolean
}>()

const expanded = ref(props.defaultExpanded ?? false)

const statusLabel = computed(() => {
  const map: Record<string, string> = {
    completed: '正常',
    partial: '部分异常',
    failed: '失败',
    running: '运行中',
  }
  return map[props.result.status] || props.result.status
})

const statusTagType = computed(() => {
  const map: Record<string, string> = {
    completed: 'success',
    partial: 'warning',
    failed: 'danger',
    running: 'info',
  }
  return map[props.result.status] || 'info'
})

const ITEM_LABELS: Record<string, string> = {
  cpu: 'CPU 使用率', memory: '内存使用', interface: '接口状态', version: '系统版本',
  routes: '路由表', log: '系统日志', environment: '环境温度', power: '电源状态',
  fan: '风扇状态', stp: 'STP 状态', vlan: 'VLAN', arp: 'ARP 表', mac: 'MAC 表',
  disk: '磁盘使用', load: '系统负载', network: '网络配置', ports: '监听端口',
  processes: '进程列表', os_version: '系统版本', logs: '异常日志',
  failed_services: '失败服务', firewall: '防火墙', security_updates: '安全更新',
  logins: '登录记录', system_info: '系统信息', services: '失败服务',
  event_logs: '事件日志', updates: '已装补丁', uptime: '运行时间',
}

function getItemLabel(type: string): string {
  return ITEM_LABELS[type] || type
}

function statusTagTypeOf(status: string): string {
  const map: Record<string, string> = { warning: 'warning', critical: 'danger', error: 'info' }
  return map[status] || 'info'
}

function statusText(status: string): string {
  const map: Record<string, string> = { warning: '警告', critical: '严重', error: '错误' }
  return map[status] || status
}

const rawDialogVisible = ref(false)
const rawDialogTitle = ref('')
const rawDialogContent = ref('')

function showRawOutput(item: InspectionItemResult) {
  rawDialogTitle.value = getItemLabel(item.item_type)
  rawDialogContent.value = item.raw_output || '(无输出)'
  rawDialogVisible.value = true
}
</script>

<style scoped>
.inspection-result-card {
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  overflow: hidden;
  margin-bottom: 12px;
  background: var(--dcn-bg-card);
  transition: border-color 0.2s;
}
.inspection-result-card.is-expanded {
  border-color: var(--dcn-border-strong);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  cursor: pointer;
  transition: background 0.15s;
}
.card-header:hover {
  background: var(--dcn-bg-muted);
}

.card-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.status-tag { flex-shrink: 0; }
.device-name { font-weight: 600; color: var(--dcn-text-primary); font-size: 14px; }
.device-ip { font-family: var(--dcn-font-mono); font-size: 12px; color: var(--dcn-text-secondary); }
.vendor-tag { flex-shrink: 0; }

.card-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.count-summary { display: flex; gap: 8px; font-size: 12px; }
.count { font-weight: 500; }
.count.normal { color: var(--el-color-success); }
.count.warning { color: var(--el-color-warning); }
.count.critical { color: var(--el-color-danger); }
.count.error { color: var(--dcn-text-secondary); }
.duration { font-size: 12px; color: var(--dcn-text-secondary); font-family: var(--dcn-font-mono); }
.expand-icon { transition: transform 0.2s; color: var(--dcn-text-secondary); }
.is-expanded .expand-icon { transform: rotate(180deg); }

.card-body { padding: 0 16px 16px; }
.error-banner {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 12px; margin-bottom: 12px;
  background: rgba(239, 68, 68, 0.1); border-radius: var(--dcn-radius-md);
  color: var(--el-color-danger); font-size: 13px;
}

.items-grid { display: flex; flex-direction: column; gap: 6px; }

.item-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 12px; border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
  transition: background 0.15s;
}
.item-row:hover { background: var(--dcn-bg-muted); }

.item-left { display: flex; align-items: center; gap: 8px; min-width: 140px; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
}
.dot-normal { background: var(--el-color-success); }
.dot-warning { background: var(--el-color-warning); }
.dot-critical { background: var(--el-color-danger); box-shadow: 0 0 6px rgba(239,68,68,0.4); }
.dot-error { background: var(--dcn-text-secondary); }
.item-label { font-size: 13px; color: var(--dcn-text-regular); }

.item-center { display: flex; align-items: center; gap: 6px; }
.item-value { font-family: var(--dcn-font-mono); font-weight: 600; color: var(--dcn-text-primary); font-size: 13px; }
.item-unit { font-size: 12px; color: var(--dcn-text-secondary); }

.item-right { display: flex; align-items: center; gap: 8px; }
.item-error { font-size: 12px; color: var(--el-color-danger); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.raw-output-pre {
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  padding: 12px 16px;
  font-family: var(--dcn-font-mono);
  font-size: 12px;
  color: var(--dcn-text-regular);
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
