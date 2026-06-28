import { ref, onUnmounted } from 'vue'

export function useFullscreen() {
  const isFullscreen = ref(false)

  function toggle() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen()
      isFullscreen.value = true
    } else {
      document.exitFullscreen()
      isFullscreen.value = false
    }
  }

  function _onFullscreenChange() {
    isFullscreen.value = !!document.fullscreenElement
  }

  if (typeof window !== 'undefined') {
    document.addEventListener('fullscreenchange', _onFullscreenChange)
  }

  onUnmounted(() => {
    document.removeEventListener('fullscreenchange', _onFullscreenChange)
  })

  return { isFullscreen, toggle }
}
