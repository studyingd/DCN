<template>
  <!-- 维护静默窗口:变更前建窗口免轰炸;窗口内事件照常流转,仅通知被抑制 -->
  <el-dialog
    v-model="windowsVisible"
    title="维护静默窗口"
    width="720px"
    class="rule-dialog"
    :close-on-click-modal="false"
  >
    <div class="mw-toolbar">
      <el-button type="primary" size="small" @click="startCreateWindow"
        ><el-icon><Plus /></el-icon>新建窗口</el-button
      >
      <small class="mw-hint">窗口内目标对象的告警通知被抑制(告警照常记录/评估),结束后推送汇总</small>
    </div>
    <el-table v-loading="windowsLoading" :data="windows" row-key="id" size="small" class="alert-table">
      <el-table-column label="窗口" min-width="180"
        ><template #default="{ row }"
          ><div class="rule-name-cell">
            <strong>{{ row.name }}</strong> <small>{{ windowStateLabel(row) }} · {{ row.target_label }}</small>
          </div></template
        ></el-table-column
      >
      <el-table-column label="时段" min-width="230"
        ><template #default="{ row }"
          ><small class="mw-time"
            >{{ formatDateTime(row.start_at) }} ~ {{ formatDateTime(row.end_at) }}</small
          ></template
        ></el-table-column
      >
      <el-table-column label="抑制" width="80" align="center"
        ><template #default="{ row }"
          ><span class="mw-muted" :class="{ active: row.muted_count > 0 }">{{ row.muted_count }}</span></template
        ></el-table-column
      >
      <el-table-column label="操作" width="150" fixed="right"
        ><template #default="{ row }"
          ><el-button v-if="row.state !== 'ended'" link type="warning" size="small" @click="finishWindow(row)"
            >提前结束</el-button
          ><el-button link type="danger" size="small" @click="removeWindow(row)">删除</el-button></template
        ></el-table-column
      >
    </el-table>
    <el-empty v-if="!windowsLoading && windows.length === 0" description="还没有静默窗口" :image-size="48" />
    <!-- 新建表单 -->
    <div v-if="windowCreating" class="mw-form">
      <el-form ref="windowFormRef" :model="windowForm" :rules="windowFormRules" label-position="top">
        <el-form-item label="窗口名称" prop="name"
          ><el-input v-model.trim="windowForm.name" placeholder="例如：重启 plm 数据服务器"
        /></el-form-item>
        <div class="form-grid">
          <el-form-item label="开始时间" prop="start_at"
            ><el-date-picker v-model="windowForm.start_at" type="datetime" style="width: 100%" /> ></el-form-item
          >
          <el-form-item label="结束时间" prop="end_at"
            ><el-date-picker v-model="windowForm.end_at" type="datetime" style="width: 100%" /> ></el-form-item
          >
        </div>
        <el-form-item label="静默范围"
          ><div class="target-scope">
            <el-radio-group v-model="windowForm.target_scope">
              <el-radio value="all">全部对象</el-radio>
              <el-radio value="selected">指定对象</el-radio>
            </el-radio-group>
            <el-button v-if="windowForm.target_scope === 'selected'" size="small" @click="windowPickerVisible = true">
              选择（设备与虚拟机 {{ windowForm.target_ids.length }} 台）
            </el-button>
          </div>
          <small class="mw-hint">建议只勾你要变更的对象——范围越小，真正的告警漏报风险越小</small>
        </el-form-item>
      </el-form>
      <div class="mw-form-actions">
        <el-button @click="windowCreating = false">取消</el-button>
        <el-button type="primary" :loading="windowSaving" @click="submitWindow">创建窗口</el-button>
      </div>
    </div>
  </el-dialog>
  <!-- 静默目标选择:复用公共 DeviceSelector -->
  <el-dialog
    v-model="windowPickerVisible"
    title="选择静默对象"
    width="860px"
    append-to-body
    class="rule-dialog"
    :close-on-click-modal="false"
  >
    <DeviceSelector :selected-ids="windowForm.target_ids" @confirm="onWindowSelectorConfirm" />
    <template #footer><el-button type="primary" @click="windowPickerVisible = false">确定</el-button></template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { alertAPI } from '@/api'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import type { MaintenanceWindow } from '@/types'
import { formatDateTime } from '@/utils/datetime'

// 维护静默窗口(从 AlertCenter.vue 拆出,2026-10-31 Phase-1):变更前建窗口
// 免轰炸;窗口内事件照常流转,仅通知被抑制,结束后推送汇总。父组件经
// ref.open() 打开,不持有内部状态。
const windowsVisible = ref(false)
const windowsLoading = ref(false)
const windows = ref<MaintenanceWindow[]>([])
const windowCreating = ref(false)
const windowSaving = ref(false)
const windowPickerVisible = ref(false)
const windowFormRef = ref<FormInstance>()
const windowForm = reactive({
  name: '',
  start_at: new Date(Date.now() - 60_000),
  end_at: new Date(Date.now() + 30 * 60_000),
  target_scope: 'all' as 'all' | 'selected',
  target_ids: [] as number[],
})
const windowFormRules: FormRules = {
  name: [{ required: true, message: '请输入窗口名称', trigger: 'blur' }],
  start_at: [{ required: true, message: '请选择开始时间', trigger: 'change' }],
  end_at: [{ required: true, message: '请选择结束时间', trigger: 'change' }],
}
// 与事件表同口径的时间展示,统一入口在 utils/datetime
const windowStateLabel = (row: MaintenanceWindow) =>
  ({ active: '静默中', pending: '未开始', ended: '已结束' })[row.state] || row.state
async function loadWindows() {
  windowsLoading.value = true
  try {
    windows.value = (await alertAPI.maintenanceWindows()).data
  } catch {
    ElMessage.error('加载静默窗口失败')
  } finally {
    windowsLoading.value = false
  }
}
async function open() {
  windowsVisible.value = true
  windowCreating.value = false
  await loadWindows()
}
function startCreateWindow() {
  Object.assign(windowForm, {
    name: '',
    start_at: new Date(Date.now() - 60_000),
    end_at: new Date(Date.now() + 30 * 60_000),
    target_scope: 'all',
    target_ids: [],
  })
  windowCreating.value = true
}
function onWindowSelectorConfirm(ids: number[]) {
  windowForm.target_ids = ids
}
async function submitWindow() {
  if (!(await windowFormRef.value?.validate().catch(() => false))) return
  if (windowForm.end_at <= windowForm.start_at) {
    ElMessage.error('结束时间必须晚于开始时间')
    return
  }
  windowSaving.value = true
  try {
    await alertAPI.createMaintenanceWindow({
      name: windowForm.name,
      target_ids: windowForm.target_scope === 'selected' && windowForm.target_ids.length ? windowForm.target_ids : null,
      start_at: windowForm.start_at.toISOString(),
      end_at: windowForm.end_at.toISOString(),
    })
    ElMessage.success('静默窗口已创建,窗口内通知将被抑制')
    windowCreating.value = false
    await loadWindows()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    windowSaving.value = false
  }
}
async function finishWindow(row: MaintenanceWindow) {
  try {
    await alertAPI.finishMaintenanceWindow(row.id)
    ElMessage.success('窗口已结束,通知已恢复(汇总稍后推送)')
    await loadWindows()
  } catch {
    ElMessage.error('操作失败')
  }
}
async function removeWindow(row: MaintenanceWindow) {
  try {
    await ElMessageBox.confirm(`删除窗口「${row.name}」？通知立即恢复,不推汇总。`, '删除静默窗口', { type: 'warning' })
    await alertAPI.deleteMaintenanceWindow(row.id)
    ElMessage.success('已删除')
    await loadWindows()
  } catch {
    /* cancelled */
  }
}

defineExpose({ open })
</script>

<style scoped>
/* 从 AlertCenter.vue 随组件迁移的样式(mw-* 专属 + 弹窗/表格共用规则的局部副本) */
:deep(.rule-dialog) {
  margin-top: 5vh !important;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 24px 70px color-mix(in srgb, #000 28%, transparent);
}
:deep(.rule-dialog .el-dialog__header) {
  padding: 20px 26px;
  border-bottom: 1px solid var(--dcn-border);
}
:deep(.rule-dialog .el-dialog__title) {
  font-size: 18px;
  font-weight: 700;
}
:deep(.rule-dialog .el-dialog__body) {
  max-height: calc(90vh - 150px);
  overflow: auto;
  padding: 20px 24px;
  background: var(--dcn-bg-page);
}
:deep(.rule-dialog .el-dialog__footer) {
  padding: 15px 24px;
  border-top: 1px solid var(--dcn-border);
  background: var(--dcn-bg-card);
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.target-scope {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  flex-wrap: wrap;
}
.target-scope :deep(.el-radio-group) {
  flex: 0 0 auto;
}
.alert-table :deep(.el-table__header-wrapper th) {
  height: 44px;
  background: var(--dcn-bg-card);
  color: var(--dcn-text-secondary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.alert-table :deep(.el-table__body tr) {
  transition: background-color 0.16s ease;
}
.alert-table :deep(.el-table__body tr:hover > td) {
  background: color-mix(in srgb, var(--dcn-primary) 5%, var(--dcn-bg-card));
}
.alert-table :deep(.el-table__cell) {
  padding: 12px 0;
}
.rule-name-cell {
  gap: 4px;
}
.rule-name-cell strong {
  color: var(--dcn-text-primary);
  font-size: 13px;
  font-weight: 650;
}
.mw-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.mw-hint {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.mw-time {
  color: var(--dcn-text-secondary);
  font: 12px var(--dcn-font-mono);
}
.mw-muted {
  color: var(--dcn-text-secondary);
  font: 600 12px var(--dcn-font-mono);
}
.mw-muted.active {
  color: var(--dcn-warning);
}
.mw-form {
  margin-top: 14px;
  padding: 16px;
  border: 1px solid var(--dcn-border);
  border-radius: 12px;
  background: var(--dcn-bg-card);
}
.mw-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
