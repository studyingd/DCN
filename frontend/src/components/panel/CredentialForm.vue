<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? '编辑凭据' : '新建凭据'"
    width="480px"
    :close-on-click-modal="false"
    @close="handleClose"
  >
    <el-form
      ref="formRef"
      :model="formData"
      :rules="formRules"
      label-width="90px"
    >
      <el-form-item label="凭据名称" prop="name">
        <el-input v-model="formData.name" placeholder="例：生产环境 Root 账号" />
      </el-form-item>
      <el-form-item label="用户名" prop="username">
        <el-input v-model="formData.username" placeholder="SSH/RDP 用户名" />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input
          v-model="formData.password"
          type="password"
          show-password
          :placeholder="isEdit ? '留空则不修改' : '请输入密码'"
        />
      </el-form-item>
      <el-form-item label="SSH 私钥" prop="ssh_key">
        <el-input
          v-model="formData.ssh_key"
          type="textarea"
          :rows="4"
          :placeholder="isEdit ? '留空则不修改' : '可选，粘贴 SSH 私钥内容'"
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
import { credentialAPI } from '@/api'
import type { Credential } from '@/types'

const props = withDefaults(
  defineProps<{
    visible: boolean
    credential?: Credential | null
  }>(),
  { credential: null }
)

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'saved'): void
}>()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const isEdit = computed(() => !!props.credential)

interface FormData {
  name: string
  username: string
  password: string
  ssh_key: string
}

const defaultForm = (): FormData => ({ name: '', username: '', password: '', ssh_key: '' })
const formData = reactive<FormData>(defaultForm())

const formRules = reactive<FormRules<FormData>>({
  name: [{ required: true, message: '请输入凭据名称', trigger: 'blur' }],
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
})

watch(
  () => props.visible,
  (val) => {
    if (val) {
      if (props.credential) {
        Object.assign(formData, {
          name: props.credential.name,
          username: props.credential.username,
          password: '',
          ssh_key: '',
        })
      } else {
        Object.assign(formData, defaultForm())
      }
      formRef.value?.clearValidate()
    }
  }
)

function handleClose() {
  formRef.value?.resetFields()
  Object.assign(formData, defaultForm())
  emit('update:visible', false)
}

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const payload: Record<string, string | undefined> = {
      name: formData.name,
      username: formData.username,
    }
    if (formData.password) payload.password = formData.password
    if (formData.ssh_key) payload.ssh_key = formData.ssh_key

    if (isEdit.value && props.credential) {
      await credentialAPI.update(props.credential.id, payload)
      ElMessage.success('凭据更新成功')
    } else {
      await credentialAPI.create(payload)
      ElMessage.success('凭据创建成功')
    }
    emit('saved')
    handleClose()
  } catch {
    ElMessage.error(isEdit.value ? '更新凭据失败' : '创建凭据失败')
  } finally {
    submitting.value = false
  }
}
</script>
