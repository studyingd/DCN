<template>
  <div class="agent-config">
    <el-alert type="info" :closable="false" show-icon class="config-tip">
      <template #title>配置保存在数据库,保存后立即生效,无需重启;未配置的项回退到 .env 中的 AGENT_* 环境变量。</template>
    </el-alert>

    <el-form v-loading="loading" label-width="130px" class="config-form">
      <el-form-item label="启用 Agent 诊断">
        <el-switch v-model="form.enabled" />
      </el-form-item>
      <el-form-item label="接口地址">
        <el-input v-model="form.base_url" placeholder="OpenAI 兼容接口,如 http://内网网关/v1" clearable />
      </el-form-item>
      <el-form-item label="API Key">
        <el-input
          v-model="form.api_key"
          type="password"
          show-password
          :placeholder="config?.api_key_set ? `已配置(${config.api_key_preview}),留空保持不变` : '请输入密钥'"
          clearable
        />
      </el-form-item>
      <el-form-item label="模型名称">
        <div class="model-row">
          <el-select
            v-model="form.model"
            placeholder="先获取模型列表,或直接手动输入"
            filterable
            allow-create
            clearable
            class="model-select"
          >
            <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
          </el-select>
          <el-button :loading="modelsLoading" :disabled="!form.base_url.trim()" @click="fetchModels">
            获取模型列表
          </el-button>
        </div>
        <div class="field-hint model-hint">API Key 留空时使用已保存的密钥;网关不支持 /models 时可手动输入</div>
      </el-form-item>
      <el-form-item label="最大诊断步数">
        <el-input-number v-model="form.max_steps" :controls="false" :min="1" :max="20" />
        <span class="field-hint">LLM 单轮诊断允许的最大工具调用轮数(1–20)</span>
      </el-form-item>

      <el-form-item>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        <el-button :loading="testing" :disabled="!config?.api_key_set && !form.api_key" @click="testConnection">
          测试连接
        </el-button>
      </el-form-item>
    </el-form>

    <el-alert
      v-if="testResult"
      :type="testResult.ok ? 'success' : 'error'"
      :title="testResult.message"
      :closable="false"
      show-icon
      class="config-tip"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { agentAPI } from '@/api'
import type { AgentConfig } from '@/types/agent'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const config = ref<AgentConfig | null>(null)
const testResult = ref<{ ok: boolean; message: string } | null>(null)
const modelOptions = ref<string[]>([])
const modelsLoading = ref(false)

const form = reactive({
  enabled: false,
  base_url: '',
  model: '',
  api_key: '',
  max_steps: 8,
})

onMounted(async () => {
  loading.value = true
  try {
    const res = await agentAPI.getConfig()
    config.value = res.data
    form.enabled = res.data.enabled
    form.base_url = res.data.base_url
    form.model = res.data.model
    form.max_steps = res.data.max_steps
    // api_key 不回填——后端只给掩码;留空表示保持不变
  } catch {
    ElMessage.error('加载 Agent 配置失败')
  } finally {
    loading.value = false
  }
})

async function save() {
  saving.value = true
  testResult.value = null
  try {
    const res = await agentAPI.updateConfig({ ...form })
    config.value = res.data
    form.api_key = ''
    ElMessage.success('配置已保存,立即生效')
  } catch {
    ElMessage.error('保存失败')
  } finally {
    saving.value = false
  }
}

async function fetchModels() {
  modelsLoading.value = true
  try {
    const res = await agentAPI.listModels(form.base_url.trim(), form.api_key.trim())
    modelOptions.value = res.data.models
    ElMessage.success(`获取到 ${res.data.models.length} 个可用模型`)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '获取模型列表失败')
  } finally {
    modelsLoading.value = false
  }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const res = await agentAPI.testConfig()
    testResult.value = { ok: true, message: res.data.message }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    testResult.value = {
      ok: false,
      message: err.response?.data?.detail || '连接失败',
    }
  } finally {
    testing.value = false
  }
}
</script>

<style scoped>
.agent-config {
  max-width: 640px;
}

.config-tip {
  margin-bottom: var(--dcn-space-4);
}

.config-form {
  margin-top: var(--dcn-space-2);
}

.field-hint {
  margin-left: var(--dcn-space-3);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

.model-row {
  display: flex;
  gap: var(--dcn-space-2);
  width: 100%;
}

.model-select {
  flex: 1;
}

.model-hint {
  margin-left: 0;
  margin-top: var(--dcn-space-1);
}
</style>
