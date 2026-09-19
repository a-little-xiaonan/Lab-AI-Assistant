<template>
  <el-container class="admin-shell" direction="vertical">
    <ModuleHeader section="admin" />
    <el-container class="admin-body">
      <el-aside width="220px" class="admin-aside">
        <div class="aside-title">
          <strong>系统管理</strong>
          <span>权限、运行状态与内容治理</span>
        </div>
        <el-menu router :default-active="route.path" class="admin-menu">
          <el-menu-item index="/admin/overview">运行概览</el-menu-item>
          <el-menu-item v-if="auth.isAdmin" index="/admin/users">用户与角色</el-menu-item>
          <el-menu-item v-if="auth.isAdmin" index="/admin/role-applications">成员申请</el-menu-item>
          <el-menu-item v-if="auth.isAdmin" index="/admin/audit-logs">审计日志</el-menu-item>
          <el-menu-item v-if="auth.isAdmin" index="/admin/evaluations">RAG 量化评测</el-menu-item>
          <el-menu-item index="/admin/jobs">后台任务</el-menu-item>
          <el-menu-item index="/admin/feedback">回答反馈</el-menu-item>
        </el-menu>
        <div class="permission-tip">
          <strong>{{ auth.isAdmin ? "管理员权限" : "实验室成员权限" }}</strong>
          <span v-if="auth.isAdmin">可管理账号、角色和全部知识库。</span>
          <span v-else>只展示内容运营信息，不能管理账号和角色。</span>
        </div>
      </el-aside>
      <el-main class="admin-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { useRoute } from "vue-router";
import ModuleHeader from "../../components/shared/ModuleHeader.vue";
import { useAuthStore } from "../../stores/auth";

const route = useRoute();
const auth = useAuthStore();
</script>

<style scoped>
.admin-shell { height: 100%; background: var(--el-bg-color-page); }
.admin-body { min-height: 0; }
.admin-aside {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color);
}
.aside-title { padding: 24px 20px 16px; display: flex; flex-direction: column; gap: 6px; }
.aside-title strong { font-size: 18px; }
.aside-title span { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.admin-menu { border-right: 0; }
.permission-tip {
  margin: auto 14px 18px;
  padding: 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  line-height: 1.5;
}
.permission-tip span { color: var(--el-text-color-secondary); }
.admin-main { padding: 24px; overflow: auto; }
@media (max-width: 720px) {
  .admin-aside { display: none; }
  .admin-main { padding: 14px; }
}
</style>
