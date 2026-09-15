<template>
  <main class="page">
    <header class="header"><el-button @click="$router.push('/chat')">← 返回聊天</el-button><h2>我的资料与记忆</h2></header>
    <el-card v-if="auth.user" class="profile"><p><strong>{{ auth.user.nickname }}</strong>（{{ auth.user.username }}）</p><p>角色：{{ auth.user.roles.join("、") }}</p></el-card>
    <el-card v-if="auth.user && !auth.isContentManager" class="memories">
      <template #header><strong>申请成为实验室成员</strong></template>
      <el-alert v-if="pending" type="warning" :closable="false" :title="`申请审核中：${pending.reason}`" />
      <template v-else>
        <el-input v-model="reason" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="请说明申请原因、参与方向或相关经历（至少 10 字）" />
        <el-input v-model="evidence" style="margin-top:8px" placeholder="补充说明（可选，不要上传敏感证明材料）" />
        <el-button type="primary" style="margin-top:10px" :disabled="reason.trim().length < 10" @click="submitApplication">提交申请</el-button>
      </template>
      <el-button v-if="pending" type="danger" plain style="margin-top:10px" @click="cancelPending">撤销申请</el-button>
      <el-timeline style="margin-top:18px">
        <el-timeline-item v-for="item in applications" :key="item.id" :timestamp="formatTime(item.created_at)">
          {{ item.status }} · {{ item.review_comment || item.reason }}
        </el-timeline-item>
      </el-timeline>
    </el-card>
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
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { clearMyMemories, deleteMyMemory, listMyMemories } from "../api/users";
import { useAuthStore } from "../stores/auth";
import type { UserMemory } from "../types";
import type { RoleApplication } from "../types";
import { applyForEditor, cancelApplication, listMyApplications } from "../api/roleApplications";

const auth = useAuthStore();
const memories = ref<UserMemory[]>([]);
const applications = ref<RoleApplication[]>([]);
const reason = ref("");
const evidence = ref("");
const pending = computed(() => applications.value.find((item) => item.status === "pending"));
const formatTime = (value: string) => new Date(value).toLocaleString();
async function load() { memories.value = await listMyMemories(); }
async function loadApplications() { applications.value = await listMyApplications(); }
async function submitApplication() { try { await applyForEditor(reason.value, evidence.value); await loadApplications(); ElMessage.success("申请已提交"); } catch (e) { ElMessage.error((e as Error).message); } }
async function cancelPending() { if (!pending.value) return; await cancelApplication(pending.value.id); await loadApplications(); ElMessage.success("申请已撤销"); }
async function remove(id: string) { await deleteMyMemory(id); await load(); ElMessage.success("已删除记忆"); }
async function clearAll() {
  try { await ElMessageBox.confirm("确定清空全部个人记忆？此操作无法恢复。", "清空记忆", { type: "warning" }); await clearMyMemories(); await load(); ElMessage.success("已清空"); } catch { /* 取消 */ }
}
onMounted(async () => { await auth.init(); if (auth.user) await Promise.all([load(), loadApplications()]); });
</script>

<style scoped>
.page { max-width: 1100px; margin: 0 auto; padding: 24px; }.header { display:flex; align-items:center; gap:16px; }.profile,.memories { margin-top:16px; }.title { display:flex; justify-content:space-between; align-items:center; }
</style>
