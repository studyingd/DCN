<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useFullscreen } from '@/composables/useFullscreen'

defineProps<{
  lastUpdated: string
}>()

const router = useRouter()
const { isFullscreen, toggle } = useFullscreen()

const now = ref('')
let timerId: ReturnType<typeof setInterval> | null = null

function updateClock() {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  now.value = `${d.getFullYear()}年${pad(d.getMonth() + 1)}月${pad(d.getDate())}日 ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function goBack() {
  router.push('/')
}

onMounted(() => {
  updateClock()
  timerId = setInterval(updateClock, 1000)
})

onUnmounted(() => {
  if (timerId !== null) clearInterval(timerId)
})
</script>

<template>
  <header class="bs-header">
    <div class="bs-header__left">
      <div class="bs-header__logo">
        <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
        </svg>
      </div>
      <span class="bs-header__title">DCN 数据中心网络运营大屏</span>
    </div>

    <div class="bs-header__center">
      <span class="bs-header__clock">{{ now }}</span>
    </div>

    <div class="bs-header__right">
      <span v-if="lastUpdated" class="bs-header__updated">更新: {{ lastUpdated }}</span>
      <button class="bs-header__btn" title="返回管理" @click="goBack">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <polyline points="9 22 9 12 15 12 15 22" />
        </svg>
        返回管理
      </button>
      <button class="bs-header__btn" :title="isFullscreen ? '退出全屏' : '全屏'" @click="toggle">
        <svg
          v-if="!isFullscreen"
          viewBox="0 0 24 24"
          width="18"
          height="18"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <polyline points="15 3 21 3 21 9" />
          <polyline points="9 21 3 21 3 15" />
          <line x1="21" y1="3" x2="14" y2="10" />
          <line x1="3" y1="21" x2="10" y2="14" />
        </svg>
        <svg v-else viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="4 14 10 14 10 20" />
          <polyline points="20 10 14 10 14 4" />
          <line x1="14" y1="10" x2="21" y2="3" />
          <line x1="3" y1="21" x2="10" y2="14" />
        </svg>
      </button>
    </div>
  </header>
</template>

<style scoped>
.bs-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
  padding: 0 var(--dcn-space-5);
  background: var(--dcn-topbar-bg);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--dcn-border);
  position: relative;
  z-index: 100;
}

.bs-header__left {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.bs-header__logo {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 1px solid rgba(96, 165, 250, 0.32);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-primary-bg);
  color: var(--dcn-primary-light);
}

.bs-header__title {
  font-size: var(--dcn-text-xl);
  font-weight: 600;
  color: var(--dcn-text-primary);
  letter-spacing: 0.6px;
  white-space: nowrap;
}

.bs-header__center {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.bs-header__clock {
  font-size: var(--dcn-text-lg);
  color: var(--dcn-text-regular);
  font-family: var(--dcn-font-mono);
  letter-spacing: 1px;
}

.bs-header__right {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
}

.bs-header__updated {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  white-space: nowrap;
}

.bs-header__btn {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) var(--dcn-space-3);
  min-height: 36px;
  background: var(--dcn-bg-section);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  color: var(--dcn-text-regular);
  font-size: var(--dcn-text-base);
  cursor: pointer;
  transition: all var(--dcn-transition-fast);
  white-space: nowrap;
}

.bs-header__btn:hover {
  background: var(--dcn-bg-muted);
  border-color: var(--dcn-border-strong);
  color: var(--dcn-text-primary);
}
</style>
