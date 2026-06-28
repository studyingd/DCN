import { createRouter, createWebHistory } from 'vue-router'
import LoginView from '@/views/LoginView.vue'
import DashboardView from '@/views/DashboardView.vue'
import TerminalView from '@/views/TerminalView.vue'
import { useAuthStore } from '@/stores/auth'

/** Only allow same-origin relative redirect targets (no open-redirect to //host). */
function safeRedirect(target: unknown): string {
  if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('//')) {
    return target
  }
  return '/'
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { requiresAuth: false },
    },
    {
      path: '/terminal',
      name: 'terminal',
      component: TerminalView,
      meta: { requiresAuth: true },
    },
    {
      path: '/',
      name: 'dashboard',
      component: DashboardView,
      meta: { requiresAuth: true },
    },
    {
      path: '/big-screen',
      name: 'big-screen',
      component: () => import('@/views/BigScreenView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // (Re)load the auth profile from the httpOnly cookie if not already loaded.
  // This covers page refresh (no localStorage token anymore) and deep links.
  if (!auth.isLoggedIn) {
    await auth.fetchProfile()
  }
  const valid = auth.isLoggedIn

  if (to.meta.requiresAuth && !valid) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && valid) {
    return { path: safeRedirect(to.query.redirect) }
  }
  return true
})

export default router
