<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">凭据管理</h2>
      <el-button type="primary" size="small" @click="openCredentialForm(null)">
        <el-icon><Plus /></el-icon>
        新建凭据
      </el-button>
    </div>
    <div class="inline-panel-body">
      <el-table :data="credentialList" stripe size="small" v-if="credentialList.length > 0">
        <el-table-column prop="name" label="名称" min-width="160" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column label="密码" width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.has_password" type="success" size="small">已设置</el-tag>
            <el-tag v-else type="info" size="small">未设置</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="SSH 密钥" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.has_ssh_key" type="success" size="small">已设置</el-tag>
            <el-tag v-else type="info" size="small">未设置</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="关联设备" width="90" align="center" prop="device_count" />
        <el-table-column label="操作" width="200" align="center">
          <template #default="{ row }">
            <el-button type="success" link size="small" @click="bindDevicesTarget = row; bindDevicesVisible = true">绑定设备</el-button>
            <el-button type="primary" link size="small" @click="openCredentialForm(row)">编辑</el-button>
            <el-button type="danger" link size="small" @click="handleDeleteCredential(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无凭据，点击上方按钮新建" :image-size="60" />
      <CredentialForm v-model:visible="credentialFormVisible" :credential="credentialFormTarget" @saved="loadCredentials" />
      <BindDevicesDialog v-model:visible="bindDevicesVisible" :credential="bindDevicesTarget" @saved="onBindDevicesSaved" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { credentialAPI } from '@/api'
import { useRoomStore } from '@/stores/room'
import { useDeviceStore } from '@/stores/device'
import CredentialForm from './CredentialForm.vue'
import BindDevicesDialog from './BindDevicesDialog.vue'
import type { Credential } from '@/types'

const roomStore = useRoomStore()
const deviceStore = useDeviceStore()
const emit = defineEmits<{ (e: 'data-changed'): void }>()

const credentialList = ref<Credential[]>([])
const credentialFormVisible = ref(false)
const credentialFormTarget = ref<Credential | null>(null)
const bindDevicesVisible = ref(false)
const bindDevicesTarget = ref<Credential | null>(null)

async function loadCredentials() {
  try {
    const res = await credentialAPI.list()
    credentialList.value = res.data
  } catch { /* ignore */ }
}

function openCredentialForm(cred: Credential | null) {
  credentialFormTarget.value = cred
  credentialFormVisible.value = true
}

async function handleDeleteCredential(cred: Credential) {
  try {
    await ElMessageBox.confirm(
      `确定要删除凭据「${cred.name}」吗？${cred.device_count > 0 ? `该凭据已关联 ${cred.device_count} 台设备，删除后设备将失去凭据绑定。` : ''}`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch { return }

  try {
    await credentialAPI.delete(cred.id)
    ElMessage.success('凭据已删除')
    loadCredentials()
    roomStore.fetchRooms()
  } catch { ElMessage.error('删除失败') }
}

async function onBindDevicesSaved() {
  loadCredentials()
  await roomStore.fetchRooms()
  // Refresh current room data so device credential_id is updated
  if (roomStore.currentRoom) {
    const freshRoom = roomStore.rooms.find(r => r.id === roomStore.currentRoom!.id)
    if (freshRoom) roomStore.setCurrentRoom(freshRoom)
  }
  // Refresh current device if selected
  if (deviceStore.currentDevice) {
    deviceStore.fetchDevice(deviceStore.currentDevice.id)
  }
  emit('data-changed')
}

onMounted(() => { loadCredentials() })
</script>

<style scoped>
/* .inline-panel styles defined globally in utilities.css */
</style>
