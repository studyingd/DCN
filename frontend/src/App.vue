<template>
  <router-view />
</template>

<script setup lang="ts">
// Root component - just renders the router view
</script>

<style>
/* Global reset styles */
*,
*::before,
*::after {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body {
  width: 100%;
  height: 100%;
  font-family: var(--dcn-font-sans);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  background-color: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
}

#app {
  width: 100%;
  height: 100%;
}

/* Scrollbar styles — handled by tokens.css */
</style>
