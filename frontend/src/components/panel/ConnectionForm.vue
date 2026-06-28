<template>
  <el-dialog
    :model-value="visible"
    title="新建连接"
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
      <el-form-item label="设备 A" prop="device_a_id">
        <el-select
          v-model="formData.device_a_id"
          placeholder="请选择设备 A"
          style="width: 100%"
          filterable
        >
          <el-option
            v-for="device in devices"
            :key="device.id"
            :label="`${device.name} (${device.ip_address})`"
            :value="device.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="设备 B" prop="device_b_id">
        <el-select
          v-model="formData.device_b_id"
          placeholder="请选择设备 B"
          style="width: 100%"
          filterable
        >
          <el-option
            v-for="device in devices"
            :key="device.id"
            :label="`${device.name} (${device.ip_address})`"
            :value="device.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="连接类型" prop="conn_type">
        <el-select v-model="formData.conn_type" placeholder="请选择连接类型" style="width: 100%">
          <el-option label="以太网" value="ethernet" />
          <el-option label="光纤" value="fiber" />
          <el-option label="串口" value="serial" />
        </el-select>
      </el-form-item>

      <el-form-item label="带宽" prop="bandwidth">
        <el-input v-model="formData.bandwidth" placeholder="例如: 1Gbps, 10Gbps" />
      </el-form-item>

      <el-form-item label="备注" prop="note">
        <el-input
          v-model="formData.note"
          type="textarea"
          :rows="2"
          placeholder="请输入备注信息"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="handleClose">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">创建</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { connectionAPI } from '@/api'
import type { Device } from '@/types'

const props = defineProps<{
  visible: boolean
  devices: Device[]
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)

interface FormData {
  device_a_id: number | undefined
  device_b_id: number | undefined
  conn_type: string
  bandwidth: string
  note: string
}

const defaultFormData = (): FormData => ({
  device_a_id: undefined,
  device_b_id: undefined,
  conn_type: '',
  bandwidth: '',
  note: '',
})

const formData = reactive<FormData>(defaultFormData())

const validateDeviceB = (_rule: unknown, value: number | undefined, callback: (error?: Error) => void) => {
  if (!value) {
    callback(new Error('请选择设备 B'))
    return
  }
  if (value === formData.device_a_id) {
    callback(new Error('设备 B 不能与设备 A 相同'))
    return
  }
  callback()
}

const formRules = reactive<FormRules<FormData>>({
  device_a_id: [{ required: true, message: '请选择设备 A', trigger: 'change' }],
  device_b_id: [{ required: true, validator: validateDeviceB, trigger: 'change' }],
  conn_type: [{ required: true, message: '请选择连接类型', trigger: 'change' }],
})

watch(
  () => props.visible,
  (val) => {
    if (val) {
      Object.assign(formData, defaultFormData())
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
    await connectionAPI.create({
      device_a_id: formData.device_a_id!,
      device_b_id: formData.device_b_id!,
      conn_type: formData.conn_type,
      bandwidth: formData.bandwidth,
      note: formData.note,
    })
    ElMessage.success('连接创建成功')
    emit('saved')
    handleClose()
  } catch {
    ElMessage.error('创建连接失败')
  } finally {
    submitting.value = false
  }
}
</script>
