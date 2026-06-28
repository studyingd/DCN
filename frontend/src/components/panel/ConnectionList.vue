<template>
  <div class="connection-list">
    <div class="connection-list__header">
      <el-button type="primary" @click="showForm = true">新建连接</el-button>
    </div>

    <el-table :data="connections" v-loading="loading" stripe border style="width: 100%">
      <el-table-column label="设备 A" min-width="120">
        <template #default="{ row }">
          {{ row.device_a_name || `设备 #${row.device_a_id}` }}
        </template>
      </el-table-column>

      <el-table-column label="设备 B" min-width="120">
        <template #default="{ row }">
          {{ row.device_b_name || `设备 #${row.device_b_id}` }}
        </template>
      </el-table-column>

      <el-table-column label="连接类型" min-width="100">
        <template #default="{ row }">
          {{ connTypeLabel(row.conn_type) }}
        </template>
      </el-table-column>

      <el-table-column prop="bandwidth" label="带宽" min-width="100" />

      <el-table-column prop="note" label="备注" min-width="120" show-overflow-tooltip />

      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-popconfirm
            title="确定要删除此连接吗？"
            confirm-button-text="确定"
            cancel-button-text="取消"
            @confirm="handleDelete(row.id)"
          >
            <template #reference>
              <el-button type="danger" link size="small">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <ConnectionForm
      :visible="showForm"
      :devices="allDevices"
      @update:visible="showForm = $event"
      @saved="handleSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { connectionAPI, deviceAPI } from '@/api'
import type { Connection, Device } from '@/types'
import ConnectionForm from './ConnectionForm.vue'

const props = defineProps<{
  deviceId?: number
}>()

const connections = ref<Connection[]>([])
const allDevices = ref<Device[]>([])
const loading = ref(false)
const showForm = ref(false)

function connTypeLabel(type: string): string {
  const map: Record<string, string> = {
    ethernet: '以太网',
    fiber: '光纤',
    serial: '串口',
  }
  return map[type] || type
}

async function loadConnections() {
  loading.value = true
  try {
    const res = await connectionAPI.list()
    let list = res.data
    if (props.deviceId) {
      list = list.filter(
        (c) => c.device_a_id === props.deviceId || c.device_b_id === props.deviceId
      )
    }
    connections.value = list
  } catch {
    ElMessage.error('加载连接列表失败')
  } finally {
    loading.value = false
  }
}

async function loadAllDevices() {
  try {
    // Load devices from the global device API (no rack scope)
    const res = await deviceAPI.list(0).catch(() => ({ data: [] as Device[] }))
    allDevices.value = res.data
  } catch {
    allDevices.value = []
  }
}

async function handleDelete(id: number) {
  try {
    await connectionAPI.delete(id)
    ElMessage.success('连接删除成功')
    await loadConnections()
  } catch {
    ElMessage.error('删除连接失败')
  }
}

function handleSaved() {
  loadConnections()
}

watch(
  () => props.deviceId,
  () => {
    loadConnections()
  }
)

onMounted(() => {
  loadConnections()
  loadAllDevices()
})
</script>

<style scoped>
.connection-list__header {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
</style>
