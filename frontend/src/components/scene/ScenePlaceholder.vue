<template>
  <div class="scene-placeholder">
    <div class="placeholder-content">
      <el-icon :size="48" color="var(--dcn-text-secondary)"><VideoCameraFilled /></el-icon>
      <p class="placeholder-text">3D 场景加载中...</p>
      <p class="placeholder-sub">Three.js 3D 可视化将在此区域渲染</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { VideoCameraFilled } from '@element-plus/icons-vue'
</script>

<style scoped>
.scene-placeholder {
  width: 100%;
  height: 100%;
  background: var(--dcn-device-host-bg);
  display: flex;
  align-items: center;
  justify-content: center;
}

.placeholder-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--dcn-space-3);
}

.placeholder-text {
  font-size: var(--dcn-text-xl);
  color: var(--dcn-text-regular);
  margin: 0;
  font-weight: 500;
}

.placeholder-sub {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
  margin: 0;
}
</style>
