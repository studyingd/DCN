<template>
  <div class="rooms-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">机房管理</h2>
      <el-button
        v-if="authStore.hasPermission('device:manage')"
        type="primary"
        size="small"
        @click="showCreateDialog = true"
      >
        <el-icon><Plus /></el-icon>
        新建机房
      </el-button>
    </div>
    <div class="rooms-panel-body">
      <el-empty v-if="rooms.length === 0" description="暂无机房，点击上方按钮新建" :image-size="60" />
      <div v-else class="room-grid">
        <button v-for="room in rooms" :key="room.id" type="button" class="room-card" @click="emit('select-room', room)">
          <div class="room-card-header">
            <el-icon :size="22" style="color: var(--dcn-primary)"><OfficeBuilding /></el-icon>
            <span class="room-name">{{ room.name }}</span>
          </div>
          <div class="room-card-meta">
            <span
              ><el-icon><Coin /></el-icon> {{ room.rack_count ?? 0 }} 个机柜</span
            >
            <span
              ><el-icon><Monitor /></el-icon> {{ countDevices(room) }} 台设备</span
            >
          </div>
          <div v-if="room.location" class="room-card-location">
            <el-icon><Location /></el-icon> {{ room.location }}
          </div>
          <div v-if="room.description" class="room-card-desc">{{ room.description }}</div>
        </button>
      </div>
    </div>

    <!-- Create Room Dialog -->
    <el-dialog v-model="showCreateDialog" title="新建机房" width="460px" :close-on-click-modal="false">
      <el-form ref="roomFormRef" :model="roomForm" :rules="roomRules" label-width="80px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="roomForm.name" placeholder="请输入机房名称" />
        </el-form-item>
        <el-form-item label="位置" prop="location">
          <el-input v-model="roomForm.location" placeholder="请输入机房位置" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="roomForm.description" type="textarea" :rows="3" placeholder="请输入机房描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreateRoom">确认创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, OfficeBuilding, Coin, Monitor, Location } from '@element-plus/icons-vue'
import { roomAPI } from '@/api'
import { useRoomStore } from '@/stores/room'
import { useAuthStore } from '@/stores/auth'
import type { Room } from '@/types'

defineProps<{ rooms: Room[] }>()
const emit = defineEmits<{
  'select-room': [room: Room]
  created: []
}>()

const roomStore = useRoomStore()
const authStore = useAuthStore()

const showCreateDialog = ref(false)
const creating = ref(false)
const roomFormRef = ref<FormInstance>()

const roomForm = reactive({
  name: '',
  location: '',
  description: '',
})

const roomRules: FormRules = {
  name: [
    { required: true, message: '请输入机房名称', trigger: 'blur' },
    { min: 1, max: 64, message: '名称长度为 1-64 个字符', trigger: 'blur' },
  ],
}

function countDevices(room: any): number {
  if (!room.racks) return 0
  return room.racks.reduce((sum: number, r: any) => sum + (r.devices?.length ?? 0), 0)
}

async function handleCreateRoom() {
  if (!roomFormRef.value) return
  const valid = await roomFormRef.value.validate().catch(() => false)
  if (!valid) return

  creating.value = true
  try {
    const res = await roomAPI.create({
      name: roomForm.name,
      location: roomForm.location,
      description: roomForm.description,
    })
    roomStore.rooms.push({ ...res.data, racks: [] })

    ElMessage.success('机房创建成功')
    showCreateDialog.value = false
    roomForm.name = ''
    roomForm.location = ''
    roomForm.description = ''
    emit('created')
  } catch {
    ElMessage.error('创建机房失败')
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.rooms-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
}

/* .inline-panel-header / .inline-panel-title 不再本地覆盖：
   旧覆盖把标题从全局的 --dcn-text-2xl(20px) 缩到 --dcn-text-xl(18px)，
   页头 padding 也从 12px 变成 16px 且丢了 min-height:64px，
   导致机房管理的标题比其他页小、页头比其他页矮。
   删掉后直接继承 utilities.css 的全局定义，与告警中心/系统设置/自动化运维一致。 */

.rooms-panel-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-5) var(--dcn-space-6);
  background: var(--dcn-bg-page);
}

.room-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--dcn-space-4);
}

.room-card {
  width: 100%;
  color: inherit;
  font: inherit;
  text-align: left;
  padding: var(--dcn-space-5);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  cursor: pointer;
  transition: all var(--dcn-transition-normal);
}
.room-card:hover {
  border-color: var(--dcn-primary);
  background: var(--dcn-primary-bg);
  transform: translateY(-2px);
  box-shadow: var(--dcn-shadow-md);
}

.room-card-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-3);
}

.room-name {
  font-size: var(--dcn-text-lg);
  font-weight: 600;
  color: var(--dcn-text-primary);
}

.room-card-meta {
  display: flex;
  gap: var(--dcn-space-5);
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
}
.room-card-meta .el-icon {
  vertical-align: -1px;
  margin-right: 2px;
}

.room-card-location {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  margin-top: var(--dcn-space-2);
}
.room-card-location .el-icon {
  vertical-align: -1px;
  margin-right: 2px;
}

.room-card-desc {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-placeholder);
  margin-top: var(--dcn-space-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
