<template>
  <el-container class="settings-page">
    <el-main>
      <el-page-header content="设置" @back="$router.push('/chat')" />
      <el-empty description="知识库管理界面将在 Phase 3-04 提供" style="margin-top: 60px" />
    </el-main>
  </el-container>
</template>

<script setup lang="ts"></script>

<style scoped>
.settings-page {
  height: 100%;
}
</style>
