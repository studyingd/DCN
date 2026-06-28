<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑机房' : '新建机房'"
    width="600px"
    @close="handleClose"
  >
    <el-form
      ref="formRef"
      :model="formData"
      :rules="formRules"
      label-width="100px"
      label-position="right"
    >
      <el-form-item label="机房名称" prop="name">
        <el-input v-model="formData.name" placeholder="请输入机房名称" />
      </el-form-item>

      <el-form-item label="位置" prop="location">
        <el-input v-model="formData.location" placeholder="请输入机房位置" />
      </el-form-item>

      <el-form-item label="描述" prop="description">
        <el-input
          v-model="formData.description"
          type="textarea"
          :rows="4"
          placeholder="请输入机房描述"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">
        {{ isEdit ? '保存' : '创建' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useRoomStore } from '@/stores/room'
import type { Room } from '@/types'

const roomStore = useRoomStore()

const props = withDefaults(
  defineProps<{
    visible: boolean
    room?: Room | null
  }>(),
  {
    room: null,
  }
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)

const isEdit = computed(() => !!props.room)

interface FormData {
  name: string
  location: string
  description: string
}

const defaultFormData = (): FormData => ({
  name: '',
  location: '',
  description: '',
})

const formData = reactive<FormData>(defaultFormData())

const formRules = reactive<FormRules<FormData>>({
  name: [{ required: true, message: '请输入机房名称', trigger: 'blur' }],
})

watch(
  () => props.visible,
  (val) => {
    if (val) {
      if (props.room) {
        Object.assign(formData, {
          name: props.room.name,
          location: props.room.location,
          description: props.room.description,
        })
      } else {
        Object.assign(formData, defaultFormData())
      }
      formRef.value?.clearValidate()
    }
  }
)

function handleClose() {
  formRef.value?.resetFields()
  Object.assign(formData, defaultFormData())
  emit('update:visible', false)
}

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const payload: Partial<Room> = {
      name: formData.name,
      location: formData.location,
      description: formData.description,
    }

    if (isEdit.value && props.room) {
      await roomStore.updateRoom(props.room.id, payload)
      ElMessage.success('机房更新成功')
    } else {
      await roomStore.createRoom(payload)
      ElMessage.success('机房创建成功')
    }

    emit('saved')
    handleClose()
  } catch {
    ElMessage.error(isEdit.value ? '更新机房失败' : '创建机房失败')
  } finally {
    submitting.value = false
  }
}
</script>
