<script setup lang="ts">
import { Monitor } from '@element-plus/icons-vue'
</script>
<template>
  <div class="terminal-placeholder">
    <div class="terminal-content">
      <el-icon :size="36" color="var(--dcn-text-secondary)"><Monitor /></el-icon>

<style scoped>
.terminal-placeholder {
  width: 100%;
  height: 100%;
  background: #1e1e1e;
  display: flex;
  align-items: center;
  justify-content: center;
}

.terminal-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--dcn-space-2);
}

.terminal-text {
  font-size: var(--dcn-text-lg);
  color: var(--dcn-text-secondary);
  margin: 0;
  font-weight: 500;
}

.terminal-sub {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-placeholder);
  margin: 0;
}
</style>
