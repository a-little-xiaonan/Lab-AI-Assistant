<template>
  <main class="page">
    <header class="header"><el-button @click="$router.push('/chat')">← 返回聊天</el-button><h2>我的资料与记忆</h2></header>
    <el-card v-if="auth.user" class="profile"><p><strong>{{ auth.user.nickname }}</strong>（{{ auth.user.username }}）</p><p>角色：{{ auth.user.roles.join("、") }}</p></el-card>
    <el-card class="memories">
      <template #header><div class="title"><strong>系统记住的信息</strong><el-button type="danger" plain size="small" :disabled="!memories.length" @click="clearAll">清空全部</el-button></div></template>
      <el-empty v-if="!memories.length" description="暂未保存长期记忆" />
      <el-table v-else :data="memories" style="width:100%">
        <el-table-column prop="memory_type" label="类型" width="110" />
        <el-table-column prop="content" label="内容" min-width="360" />
        <el-table-column label="更新时间" width="180"><template #default="{ row }">{{ formatTime(row.updated_at) }}</template></el-table-column>
        <el-table-column label="操作" width="90"><template #default="{ row }"><el-button link type="danger" @click="remove(row.id)">删除</el-button></template></el-table-column>
      </el-table>
    </el-card>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { clearMyMemories, deleteMyMemory, listMyMemories } from "../api/users";
import { useAuthStore } from "../stores/auth";
import type { UserMemory } from "../types";

const auth = useAuthStore();
const memories = ref<UserMemory[]>([]);
const formatTime = (value: string) => new Date(value).toLocaleString();
async function load() { memories.value = await listMyMemories(); }
async function remove(id: string) { await deleteMyMemory(id); await load(); ElMessage.success("已删除记忆"); }
async function clearAll() {
  try { await ElMessageBox.confirm("确定清空全部个人记忆？此操作无法恢复。", "清空记忆", { type: "warning" }); await clearMyMemories(); await load(); ElMessage.success("已清空"); } catch { /* 取消 */ }
}
onMounted(async () => { await auth.init(); if (auth.user) await load(); });
</script>

<style scoped>
.page { max-width: 1100px; margin: 0 auto; padding: 24px; }.header { display:flex; align-items:center; gap:16px; }.profile,.memories { margin-top:16px; }.title { display:flex; justify-content:space-between; align-items:center; }
</style>
