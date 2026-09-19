<template>
  <section class="admin-users">
    <div class="page-heading">
      <div>
        <h1>用户与角色</h1>
        <p>管理用户状态与系统角色，仅管理员可执行。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>
      <el-alert title="普通学生仅使用问答；实验室成员可管理内容；管理员可管理用户、知识库与授权。" type="info" :closable="false" show-icon style="margin-bottom: 16px" />
      <el-table v-loading="loading" :data="users" stripe>
        <el-table-column prop="username" label="账号" min-width="130" />
        <el-table-column prop="nickname" label="昵称" min-width="120" />
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column label="角色" min-width="230">
          <template #default="{ row }">
            <el-checkbox-group :model-value="row.roles" @change="saveRoles(row, Array.isArray($event) ? $event.map(String) : [])">
              <el-checkbox label="student">普通学生</el-checkbox><el-checkbox label="editor">实验室成员</el-checkbox><el-checkbox label="admin">管理员</el-checkbox>
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
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { listUsers, updateUserRoles, updateUserStatus } from "../../../api/identity/admin";
import { useAuthStore } from "../../../stores/auth";
import type { AuthUser } from "../../../types";

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
.admin-users { max-width: 1200px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 18px; }
.page-heading h1 { margin: 0 0 6px; font-size: 24px; }
.page-heading p { margin: 0; color: var(--el-text-color-secondary); }
</style>
