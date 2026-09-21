<template>
  <div class="inspection-result-card" :class="{ 'is-expanded': expanded }">
    <!-- Header -->
    <button class="card-header" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
      <div class="card-header-left">
        <el-tag :type="statusTagType" size="small" effect="dark" round class="status-tag">
          {{ statusLabel }}
        </el-tag>
        <span class="device-name">{{ result.device_name }}</span>
        <span class="device-ip">{{ result.ip_address || '-' }}</span>
        <el-tag size="small" :type="result.target_type === 'linux' ? 'success' : 'primary'" class="target-type-tag">
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
    </button>

    <!-- Expanded item list -->
    <div v-if="expanded" class="card-body">
      <div v-if="result.error" class="error-banner">
        <el-icon><WarningFilled /></el-icon>
        {{ result.error }}
      </div>
      <div class="items-grid">
        <div v-for="item in result.items" :key="item.item_type" class="item-row" :class="'item-' + item.status">
          <div class="item-left">
            <span class="status-dot" :class="'dot-' + item.status" />
            <span class="item-label">{{ getItemLabel(item.item_type) }}</span>
          </div>
          <div class="item-center">
            <!-- value 为空（命令失败 / 无法解析）时给占位符，并且不渲染单位。
                 否则失败项会显示成一个孤零零的「%」，看起来像排版坏了。 -->
            <template v-if="item.value">
              <span class="item-value">{{ item.value }}</span>
              <span v-if="item.unit" class="item-unit">{{ item.unit }}</span>
            </template>
            <span v-else class="item-value item-value-empty">—</span>
            <el-tag v-if="item.status !== 'normal'" :type="statusTagTypeOf(item.status)" size="small" effect="plain">
              {{ statusText(item.status) }}
            </el-tag>
          </div>
          <div class="item-right">
            <el-button v-if="item.raw_output" type="primary" link size="small" @click.stop="showRawOutput(item)">
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
import { getItemLabel } from '@/utils/inspectionLabels'

const props = defineProps<{
  result: InspectionDeviceResult
}>()

// 多台结果一律默认收起：卡片头部已经带了状态、设备名、IP 与 ✓/⚠/✕/? 计数，
// 扫一眼就能定位异常机器，再按需展开。以前只有第一台展开（idx === 0），
// 批量巡检时首台内容会把其余设备挤出屏幕，而且“首台特殊”本身也不一致。
const expanded = ref(false)

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

// item_type → 中文名统一由 @/utils/inspectionLabels 提供（键集与后端 item_type 对齐，
// 由 backend/tests/test_inspection_label_parity.py 钉住，避免两边漂移）。

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
  width: 100%;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
}
.card-header:hover {
  background: var(--dcn-bg-muted);
}

.card-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.status-tag {
  flex-shrink: 0;
}
.device-name {
  font-weight: 600;
  color: var(--dcn-text-primary);
  font-size: 14px;
}
.device-ip {
  font-family: var(--dcn-font-mono);
  font-size: 12px;
  color: var(--dcn-text-secondary);
}
.target-type-tag {
  flex-shrink: 0;
}

.card-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.count-summary {
  display: flex;
  gap: 8px;
  font-size: 12px;
}
.count {
  font-weight: 500;
}
.count.normal {
  color: var(--el-color-success);
}
.count.warning {
  color: var(--el-color-warning);
}
.count.critical {
  color: var(--el-color-danger);
}
.count.error {
  color: var(--dcn-text-secondary);
}
.duration {
  font-size: 12px;
  color: var(--dcn-text-secondary);
  font-family: var(--dcn-font-mono);
}
.expand-icon {
  transition: transform 0.2s;
  color: var(--dcn-text-secondary);
}
.is-expanded .expand-icon {
  transform: rotate(180deg);
}

.card-body {
  padding: 0 16px 16px;
}
.error-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  margin-bottom: 12px;
  background: rgba(239, 68, 68, 0.1);
  border-radius: var(--dcn-radius-md);
  color: var(--el-color-danger);
  font-size: 13px;
}

.items-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.item-row {
  /* 三列 grid：两侧等宽 1fr，中间 auto。
     这样中间数值永远水平居中，不会因为右列「原始输出」按钮
     有无（raw_output 为空时按钮不渲染）而左右偏移。 */
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  padding: 8px 12px;
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
  transition: background 0.15s;
}
.item-row:hover {
  background: var(--dcn-bg-muted);
}

.item-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 140px;
  justify-self: start;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-normal {
  background: var(--el-color-success);
}
.dot-warning {
  background: var(--el-color-warning);
}
.dot-critical {
  background: var(--el-color-danger);
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.4);
}
.dot-error {
  background: var(--dcn-text-secondary);
}
.item-label {
  font-size: 13px;
  color: var(--dcn-text-regular);
}

.item-center {
  display: flex;
  align-items: center;
  gap: 6px;
  justify-self: center;
}
.item-value {
  font-family: var(--dcn-font-mono);
  font-weight: 600;
  color: var(--dcn-text-primary);
  font-size: 13px;
}
.item-unit {
  font-size: 12px;
  color: var(--dcn-text-secondary);
}
/* 失败项的占位符：不加粗、用次要色，避免被误读成一个真实读数 */
.item-value-empty {
  font-weight: 400;
  color: var(--dcn-text-secondary);
}

.item-right {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-self: end;
}
.item-error {
  font-size: 12px;
  color: var(--el-color-danger);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

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
