import { createRouter, createWebHistory } from 'vue-router'
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
      component: () => import('@/views/LoginView.vue'),
      meta: { requiresAuth: false },
    },
    {
      path: '/terminal',
      name: 'terminal',
      component: () => import('@/views/TerminalView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
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
  const requiresAuth = to.matched.some((record) => record.meta.requiresAuth)

  if (to.name === 'login') {
    if (auth.isLoggedIn) {
      return { path: safeRedirect(to.query.redirect) }
    }
    return true
  }

  // Only protected routes should probe the httpOnly-cookie session. Public
  // pages must not trigger the 401 -> refresh -> login redirect cycle.
  if (requiresAuth && !auth.isLoggedIn) {
    const valid = await auth.fetchProfile()
    if (valid) return true
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  return true
})

export default router
