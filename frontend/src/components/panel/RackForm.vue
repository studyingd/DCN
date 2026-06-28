<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑机柜' : '新建机柜'"
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
      <el-form-item label="名称" prop="name">
        <el-input v-model="formData.name" placeholder="请输入机柜名称" />
      </el-form-item>

      <el-form-item label="类型" prop="type">
        <el-radio-group v-model="formData.type">
          <el-radio value="cabinet">机柜</el-radio>
          <el-radio value="shelf">货架</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="formData.type === 'cabinet'" label="容量 (U)" prop="capacity_u">
        <el-input-number v-model="formData.capacity_u" :min="1" :max="100" />
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
import { rackAPI } from '@/api'
import type { Rack } from '@/types'

const props = withDefaults(
  defineProps<{
    visible: boolean
    roomId: number
    rack?: Rack | null
  }>(),
  {
    rack: null,
  }
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)

const isEdit = computed(() => !!props.rack)

interface FormData {
  name: string
  type: 'cabinet' | 'shelf'
  capacity_u: number | null
}

const defaultFormData = (): FormData => ({
  name: '',
  type: 'cabinet',
  capacity_u: 42,
})

const formData = reactive<FormData>(defaultFormData())

const formRules = reactive<FormRules<FormData>>({
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  type: [{ required: true, message: '请选择类型', trigger: 'change' }],
})

watch(
  () => props.visible,
  (val) => {
    if (val) {
      if (props.rack) {
        Object.assign(formData, {
          name: props.rack.name,
          type: props.rack.type,
          capacity_u: props.rack.capacity_u ?? 42,
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
    const payload: Partial<Rack> = {
      name: formData.name,
      type: formData.type,
      capacity_u: formData.type === 'cabinet' ? formData.capacity_u : null,
    }

    if (isEdit.value && props.rack) {
      await rackAPI.update(props.rack.id, payload)
      ElMessage.success('机柜更新成功')
    } else {
      await rackAPI.create(props.roomId, payload)
      ElMessage.success('机柜创建成功')
    }

    emit('saved')
    handleClose()
  } catch {
    ElMessage.error(isEdit.value ? '更新机柜失败' : '创建机柜失败')
  } finally {
    submitting.value = false
  }
}
</script>
