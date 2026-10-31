<template>
  <section class="automation-page history-page">
    <div class="atm-toolbar">
      <el-select :model-value="jobType" clearable placeholder="任务类型" @update:model-value="onTypeChange"
        ><el-option v-for="type in types" :key="type.key" :label="type.label" :value="type.key"
      /></el-select>
      <el-select :model-value="status" clearable placeholder="执行状态" @update:model-value="onStatusChange"
        ><el-option label="等待中" value="pending" /><el-option label="执行中" value="running" /><el-option
          label="已完成"
          value="completed" /><el-option label="部分异常" value="partial" /><el-option label="执行失败" value="failed"
      /></el-select>
      <el-button :loading="loading" @click="emit('refresh')"
        ><el-icon><Refresh /></el-icon>刷新</el-button
      >
    </div>
    <el-table v-loading="loading" :data="jobs" row-key="id" class="job-table" @row-click="onRowClick">
      <el-table-column label="任务" min-width="220"
        ><template #default="{ row }"
          ><div class="table-job">
            <span class="atm-icon-box atm-job-type-icon" :class="`atm-icon--${jobMetaFor(row.job_type).tone}`"
              ><el-icon><component :is="jobMetaFor(row.job_type).icon" /></el-icon></span
            ><span
              ><strong>{{ row.name }}</strong
              ><small>#{{ row.id }} · {{ jobMetaFor(row.job_type).label }}</small></span
            >
          </div></template
        ></el-table-column
      >
      <el-table-column label="目标" width="100" align="center"
        ><template #default="{ row }">{{ row.target_total }} 台</template></el-table-column
      >
      <el-table-column label="进度" min-width="150"
        ><template #default="{ row }"
          ><el-progress :percentage="jobProgress(row)" :status="progressStatus(row.status)" /></template
      ></el-table-column>
      <el-table-column label="状态" width="110" align="center"
        ><template #default="{ row }"
          ><el-tag :type="jobStatusTagType(row.status)" effect="plain">{{
            jobStatusLabel(row.status)
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column prop="created_by_name" label="执行人" width="120" show-overflow-tooltip /><el-table-column
        label="创建时间"
        width="180"
        ><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column
      >
      <el-table-column label="操作" width="80" align="center"
        ><template #default="{ row }"
          ><el-button link type="primary" @click.stop="emit('open-detail', row.id)">详情</el-button></template
        ></el-table-column
      >
    </el-table>
    <div v-if="total > pageSize" class="pagination-row">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="prev, pager, next"
        @update:current-page="onPageChange"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { Refresh } from '@element-plus/icons-vue'
import { jobProgress } from '@/utils/automationProgress'
import { jobStatusLabel, jobStatusTagType, progressStatus } from '@/utils/agentRunText'
import { formatDateTime } from '@/utils/datetime'
import { jobTypeMeta, type JobTypeMeta } from './jobTypes'
import type { AutomationJobListItem, AutomationJobType } from '@/types/automation'
import './automation.css'

defineProps<{
  jobs: AutomationJobListItem[]
  total: number
  loading: boolean
  page: number
  pageSize: number
  types: JobTypeMeta[]
  jobType: AutomationJobType | ''
  status: string
}>()

const emit = defineEmits<{
  (e: 'update:jobType', value: AutomationJobType | ''): void
  (e: 'update:status', value: string): void
  (e: 'update:page', value: number): void
  (e: 'refresh'): void
  (e: 'open-detail', id: number): void
}>()

function jobMetaFor(type: string) {
  return jobTypeMeta[type as keyof typeof jobTypeMeta]
}
// 筛选变化时回到第 1 页再刷新
function onTypeChange(value: AutomationJobType | '') {
  emit('update:jobType', value)
  emit('update:page', 1)
  emit('refresh')
}
function onStatusChange(value: string) {
  emit('update:status', value)
  emit('update:page', 1)
  emit('refresh')
}
function onPageChange(value: number) {
  emit('update:page', value)
  emit('refresh')
}
function onRowClick(row: AutomationJobListItem) {
  emit('open-detail', row.id)
}
</script>

<style scoped>
.job-table {
  cursor: pointer;
}
.table-job {
  display: flex;
  align-items: center;
  gap: 10px;
}
.table-job > span:last-child {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.table-job small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.pagination-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
