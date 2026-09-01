<template>
  <el-container class="admin-page">
    <el-header class="admin-header">
      <el-page-header content="用户与角色管理" @back="$router.push('/chat')" />
      <el-button :loading="loading" @click="load">刷新</el-button>
    </el-header>
    <el-main>
      <el-alert title="student 只能使用获授权知识库；editor 可管理内容；admin 可管理用户、知识库与授权。" type="info" :closable="false" show-icon style="margin-bottom: 16px" />
      <el-table v-loading="loading" :data="users" stripe>
        <el-table-column prop="username" label="账号" min-width="130" />
        <el-table-column prop="nickname" label="昵称" min-width="120" />
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column label="角色" min-width="230">
          <template #default="{ row }">
            <el-checkbox-group :model-value="row.roles" @change="saveRoles(row, Array.isArray($event) ? $event.map(String) : [])">
              <el-checkbox label="student">学生</el-checkbox><el-checkbox label="editor">编辑</el-checkbox><el-checkbox label="admin">管理员</el-checkbox>
            </el-checkbox-group>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="160">
          <template #default="{ row }">
            <el-switch :model-value="row.status === 'active'" active-text="正常" inactive-text="禁用" :disabled="row.id === auth.user?.id" @change="saveStatus(row, Boolean($event))" />
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
      </el-table>
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { listUsers, updateUserRoles, updateUserStatus } from "../api/admin";
import { useAuthStore } from "../stores/auth";
import type { AuthUser } from "../types";

const auth = useAuthStore();
const users = ref<AuthUser[]>([]);
const loading = ref(false);
const formatTime = (time: string) => new Date(time).toLocaleString("zh-CN", { hour12: false });

async function load() {
  loading.value = true;
  try { users.value = await listUsers(); } catch (error) { ElMessage.error((error as Error).message); } finally { loading.value = false; }
}
async function saveRoles(row: AuthUser, roles: string[]) {
  if (!roles.length) { ElMessage.warning("每个用户至少保留一个角色"); return; }
  try { Object.assign(row, await updateUserRoles(row.id, roles)); ElMessage.success("角色已更新"); }
  catch (error) { ElMessage.error((error as Error).message); await load(); }
}
async function saveStatus(row: AuthUser, active: boolean) {
  try { Object.assign(row, await updateUserStatus(row.id, active ? "active" : "disabled")); ElMessage.success("账号状态已更新"); }
  catch (error) { ElMessage.error((error as Error).message); await load(); }
}
onMounted(load);
</script>

<style scoped>
.admin-page { height: 100%; }
.admin-header { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--el-border-color-light); }
</style>
