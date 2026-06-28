<template>
  <el-dialog
    :model-value="visible"
    title="绑定设备"
    width="520px"
    :close-on-click-modal="false"
    @close="handleClose"
  >
    <p style="margin: 0 0 12px; color: #909399; font-size: 13px;">
      选择要绑定此凭据的设备，绑定后 SSH/RDP 连接将自动使用该凭据，无需再输入密码。
    </p>
    <el-table
      ref="tableRef"
      :data="devices"
      stripe
      size="small"
      max-height="400"
      @selection-change="onSelectionChange"
    >
      <el-table-column type="selection" width="45" />
      <el-table-column prop="name" label="设备名称" min-width="130" />
      <el-table-column prop="ip_address" label="IP 地址" width="140">
        <template #default="{ row }">{{ row.ip_address || '-' }}</template>
      </el-table-column>
      <el-table-column prop="type" label="类型" width="80">
        <template #default="{ row }">{{ deviceTypeLabel(row.type) }}</template>
      </el-table-column>
      <el-table-column label="当前凭据" width="90" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.credential_id && row.credential_id !== credentialId" type="warning" size="small">
            已绑定其他
          </el-tag>
          <el-tag v-else-if="row.credential_id === credentialId" type="success" size="small">
            已绑定
          </el-tag>
          <span v-else style="color: #c0c4cc;">-</span>
        </template>
      </el-table-column>
    </el-table>

    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSave">
        确认绑定 ({{ selectedIds.length }})
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type ElTable from 'element-plus/es/components/table/index'
import { credentialAPI } from '@/api'
import type { Credential } from '@/types'

interface BindDevice {
  id: number
  name: string
  ip_address: string | null
  type: string
  credential_id: number | null
  rack_id: number
}

const props = defineProps<{
  visible: boolean
  credential: Credential | null
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const tableRef = ref<InstanceType<typeof ElTable>>()
const devices = ref<BindDevice[]>([])
const selectedIds = ref<number[]>([])
const submitting = ref(false)

const credentialId = ref<number>(0)

function deviceTypeLabel(type: string): string {
  const map: Record<string, string> = {
    server: '服务器', switch: '交换机', router: '路由器',
    firewall: '防火墙', host: '主机',
  }
  return map[type.toLowerCase()] ?? type
}

watch(
  () => props.visible,
  async (val) => {
    if (val && props.credential) {
      credentialId.value = props.credential.id
      try {
        const res = await credentialAPI.allDevicesForBinding()
        devices.value = res.data
        // Pre-select devices already bound to this credential
        const boundIds = devices.value
          .filter(d => d.credential_id === props.credential!.id)
          .map(d => d.id)
        selectedIds.value = boundIds

        // Use nextTick to wait for table render, then toggle rows
        await Promise.resolve()
        devices.value.forEach(row => {
          if (boundIds.includes(row.id)) {
            tableRef.value?.toggleRowSelection(row, true)
          }
        })
      } catch { /* ignore */ }
    }
  }
)

function onSelectionChange(rows: BindDevice[]) {
  selectedIds.value = rows.map(r => r.id)
}

async function handleSave() {
  if (!props.credential) return
  submitting.value = true
  try {
    await credentialAPI.bindDevices(props.credential.id, selectedIds.value)
    ElMessage.success('设备绑定已更新')
    emit('saved')
    handleClose()
  } catch {
    ElMessage.error('绑定失败')
  } finally {
    submitting.value = false
  }
}

function handleClose() {
  devices.value = []
  selectedIds.value = []
  emit('update:visible', false)
}
</script>
