<script setup lang="ts">
import SideNav from './SideNav.vue'
import TopBar from './TopBar.vue'
</script>

<template>
  <div class="app-shell">
    <SideNav />
    <div class="app-frame">
      <TopBar />
      <main id="main-content" class="workspace" tabindex="-1">
        <slot />
      </main>
    </div>
  </div>
</template>
