<template>
  <div class="login-container">
    <!-- Brand radial gradient backdrop -->
    <div class="login-backdrop" aria-hidden="true" />

    <div class="login-card">
      <div class="login-header">
        <div class="logo-icon">
          <el-icon :size="36"><Monitor /></el-icon>
        </div>
        <h1 class="login-title">DCN</h1>
        <p class="login-subtitle">数据中心网络可视化平台</p>
      </div>

      <el-form
        ref="formRef"
        :model="loginForm"
        :rules="rules"
        label-position="top"
        size="large"
        @submit.prevent="handleLogin"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="loginForm.username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            autocomplete="username"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            class="login-button"
            native-type="submit"
          >
            <span v-if="loading">登录中...</span>
            <span v-else>登 录</span>
          </el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="login-footer">
      <span>DCN &copy; {{ new Date().getFullYear() }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Monitor } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const formRef = ref<FormInstance>()
const loading = ref(false)

const loginForm = reactive({
  username: '',
  password: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 2, max: 32, message: '用户名长度为 2-32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 4, max: 64, message: '密码长度为 4-64 个字符', trigger: 'blur' },
  ],
}

async function handleLogin() {
  if (!formRef.value) return

  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const success = await authStore.login(loginForm.username, loginForm.password)
    if (success) {
      ElMessage.success('登录成功')
      // Only allow same-origin relative redirects (prevent open redirect).
      const raw = route.query.redirect as string
      const redirect = typeof raw === 'string' && raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
      router.push(redirect)
    } else {
      ElMessage.error('用户名或密码错误')
    }
  } catch {
    ElMessage.error('登录失败，请稍后重试')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--dcn-login-bg);
  padding: var(--dcn-space-5);
  position: relative;
  overflow: hidden;
}

/* Brand radial gradient — matches Ongrid login */
.login-backdrop {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 30% 20%, rgba(140, 109, 240, 0.12), transparent 55%),
    radial-gradient(circle at 70% 80%, rgba(48, 166, 208, 0.10), transparent 55%);
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: rgba(23, 23, 32, 0.80);
  backdrop-filter: blur(16px);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-2xl);
  padding: var(--dcn-space-8) 32px var(--dcn-space-6);
  box-shadow: var(--dcn-login-card-shadow);
  position: relative;
  z-index: 1;
}

.login-header {
  text-align: center;
  margin-bottom: var(--dcn-space-6);
}

.logo-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  background: linear-gradient(135deg, #8C6DF0, #5269F4);
  border-radius: 16px;
  color: #fff;
  margin-bottom: var(--dcn-space-4);
  box-shadow: 0 8px 24px rgba(140, 109, 240, 0.3);
}

.login-title {
  margin: 0 0 var(--dcn-space-2);
  font-size: var(--dcn-text-3xl);
  font-weight: 700;
  color: var(--dcn-text-primary);
  letter-spacing: 3px;
}

.login-subtitle {
  margin: 0;
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}

/* Custom submit button — brand gradient */
.login-button {
  width: 100%;
  margin-top: var(--dcn-space-2);
  height: 42px;
  font-size: var(--dcn-text-lg);
  font-weight: 500;
  letter-spacing: 4px;
  border: none !important;
  border-radius: var(--dcn-radius-lg) !important;
  background: var(--dcn-login-btn-gradient) !important;
  color: #fff !important;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: 0 4px 14px rgba(140, 109, 240, 0.3);
}
.login-button:hover,
.login-button:focus {
  filter: brightness(1.1);
  box-shadow: 0 6px 20px rgba(140, 109, 240, 0.4);
  background: var(--dcn-login-btn-gradient) !important;
  color: #fff !important;
  border: none !important;
}

.login-footer {
  margin-top: var(--dcn-space-8);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-sm);
  position: relative;
  z-index: 1;
}

/* Form label styling */
:deep(.el-form-item__label) {
  font-weight: 500;
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
}
:deep(.el-input__wrapper) {
  background: rgba(11, 11, 17, 0.4);
  border-radius: var(--dcn-radius-lg);
  box-shadow: 0 0 0 1px var(--dcn-border) inset;
}
:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--dcn-border-strong) inset;
}
:deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--dcn-border-strong) inset;
}
:deep(.el-input__inner) {
  color: var(--dcn-text-primary);
}
:deep(.el-input__inner::placeholder) {
  color: var(--dcn-text-placeholder);
}
</style>
