<template>
  <div class="login-container">
    <div class="login-backdrop" aria-hidden="true" />
    <main class="login-shell">
      <section class="login-brand-panel" aria-label="产品介绍">
        <div class="brand-badge">
          <el-icon><Monitor /></el-icon><span>DCN</span>
        </div>
        <div class="brand-message">
          <span class="brand-kicker">DATA CENTER OPERATIONS</span>
          <h1>让基础设施状态<br />清晰、可控、可追溯</h1>
          <p>统一管理机房、服务器、容器、虚拟化平台与业务服务，快速定位异常并执行运维操作。</p>
        </div>
        <div class="brand-status"><span class="brand-status-dot" />安全运维访问入口</div>
      </section>

      <section class="login-card" aria-labelledby="login-title">
        <div class="login-header">
          <div class="logo-icon">
            <el-icon :size="24"><Monitor /></el-icon>
          </div>
          <div>
            <h2 id="login-title" class="login-title">登录控制台</h2>
            <p class="login-subtitle">请使用已授权的管理员账户继续</p>
          </div>
        </div>

        <el-alert v-if="loginError" class="login-error" type="error" :title="loginError" :closable="false" show-icon />

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
              @input="loginError = ''"
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
              @input="loginError = ''"
            />
          </el-form-item>
          <el-form-item class="login-submit-item">
            <el-button type="primary" :loading="loading" class="login-button" native-type="submit">
              <span>{{ loading ? '正在登录...' : '登录' }}</span>
            </el-button>
          </el-form-item>
        </el-form>
        <div class="login-card-footer">DCN Operations Console · {{ new Date().getFullYear() }}</div>
      </section>
    </main>
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
const loginError = ref('')

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
  loginError.value = ''
  try {
    await authStore.login(loginForm.username, loginForm.password)
    ElMessage.success('登录成功')
    // Only allow same-origin relative redirects (prevent open redirect).
    const raw = route.query.redirect as string
    const redirect = typeof raw === 'string' && raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
    router.push(redirect)
  } catch (error: unknown) {
    const response = (error as { response?: { status?: number; data?: { detail?: string } } }).response
    if (response?.status === 401) {
      loginError.value = '用户名或密码错误'
    } else if (response?.status === 403 || response?.status === 429) {
      loginError.value = response.data?.detail || '当前账户暂时无法登录'
    } else {
      loginError.value = '登录服务异常，请稍后重试'
    }
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
  padding: 32px;
  position: relative;
  overflow: hidden;
}

/* Brand radial gradient — matches Ongrid login */
.login-backdrop {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(rgba(59, 130, 246, 0.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(59, 130, 246, 0.025) 1px, transparent 1px),
    radial-gradient(circle at 15% 15%, rgba(59, 130, 246, 0.15), transparent 36%),
    radial-gradient(circle at 85% 85%, rgba(56, 189, 248, 0.08), transparent 34%);
  background-size:
    32px 32px,
    32px 32px,
    auto,
    auto;
}

.login-shell {
  display: grid;
  grid-template-columns: minmax(420px, 1.2fr) minmax(360px, 440px);
  width: min(1080px, 100%);
  min-height: 600px;
  overflow: hidden;
  border: 1px solid var(--dcn-border);
  border-radius: 18px;
  background: rgba(8, 13, 23, 0.76);
  box-shadow: var(--dcn-login-card-shadow);
  position: relative;
  z-index: 1;
}

.login-brand-panel {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 44px 48px;
  border-right: 1px solid var(--dcn-border-light);
  background: linear-gradient(145deg, rgba(37, 99, 235, 0.13), rgba(8, 13, 23, 0.16));
}
.brand-badge {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  width: fit-content;
  color: var(--dcn-primary-light);
}
.brand-badge .el-icon {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid rgba(96, 165, 250, 0.35);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-primary-bg);
  font-size: 18px;
}
.brand-badge span {
  font: 600 17px var(--dcn-font-mono);
  letter-spacing: 0.14em;
}
.brand-message {
  max-width: 560px;
}
.brand-kicker {
  color: var(--dcn-primary-light);
  font: 600 10px var(--dcn-font-mono);
  letter-spacing: 0.18em;
}
.brand-message h1 {
  margin: 14px 0 18px;
  font-size: clamp(34px, 4vw, 50px);
  line-height: 1.18;
  letter-spacing: -0.035em;
}
.brand-message p {
  max-width: 520px;
  color: var(--dcn-text-secondary);
  font-size: 15px;
  line-height: 1.8;
}
.brand-status {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.brand-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--dcn-success);
  box-shadow: 0 0 8px rgba(34, 197, 94, 0.55);
}

.login-card {
  display: flex;
  flex-direction: column;
  justify-content: center;
  width: 100%;
  padding: 48px 42px;
  background: var(--dcn-login-card-bg);
}

.login-header {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: 30px;
}

.login-error {
  margin-bottom: var(--dcn-space-4);
}

.logo-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  flex-shrink: 0;
  background: var(--dcn-primary-bg-deep);
  border: 1px solid rgba(96, 165, 250, 0.32);
  border-radius: var(--dcn-radius-lg);
  color: #fff;
  box-shadow: none;
}

.login-title {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 600;
  color: var(--dcn-text-primary);
  letter-spacing: -0.02em;
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
  font-size: var(--dcn-text-md);
  font-weight: 600;
  letter-spacing: 0;
  border: none !important;
  border-radius: var(--dcn-radius-lg) !important;
  background: var(--dcn-login-btn-gradient) !important;
  color: #fff !important;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.24);
}
.login-button:hover,
.login-button:focus {
  filter: brightness(1.1);
  box-shadow: 0 10px 24px rgba(37, 99, 235, 0.32);
  background: var(--dcn-login-btn-gradient) !important;
  color: #fff !important;
  border: none !important;
}

.login-card-footer {
  margin-top: var(--dcn-space-5);
  padding-top: var(--dcn-space-4);
  border-top: 1px solid var(--dcn-border-light);
  color: var(--dcn-text-secondary);
  font-size: 10px;
  text-align: center;
}
.login-submit-item {
  margin-bottom: 0;
}

/* Form label styling */
:deep(.el-form-item__label) {
  font-weight: 500;
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-sm);
}
:deep(.el-input__wrapper) {
  background: var(--dcn-input-bg);
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

@media (max-width: 860px) {
  .login-container {
    padding: var(--dcn-space-4);
  }
  .login-shell {
    grid-template-columns: 1fr;
    min-height: 0;
    max-width: 460px;
  }
  .login-brand-panel {
    display: none;
  }
  .login-card {
    padding: 36px 28px;
  }
}
</style>
