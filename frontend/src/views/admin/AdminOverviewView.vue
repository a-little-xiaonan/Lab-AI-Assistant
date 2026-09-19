<template>
  <section class="overview">
    <div class="page-heading">
      <div>
        <h1>运行概览</h1>
        <p>{{ auth.isAdmin ? "查看系统用户与全部知识库运行情况" : "查看你可以维护的知识库运行情况" }}</p>
      </div>
      <el-button :loading="loading" @click="load">刷新数据</el-button>
    </div>

    <el-row :gutter="16" class="metrics">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="metric"><strong>{{ stats.knowledge_base_count }}</strong><span>可管理知识库</span></div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="metric"><strong>{{ stats.document_count }}</strong><span>文档总数</span></div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="metric"><strong>{{ stats.chunk_count }}</strong><span>Chunk 总数</span></div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="metric"><strong>{{ auth.isAdmin ? users.length : "—" }}</strong><span>系统用户</span></div></el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="15">
        <el-card shadow="never">
          <template #header><strong>知识库运行情况</strong></template>
          <el-empty v-if="!stats.knowledge_bases.length" description="暂无可管理知识库" />
          <el-table v-else :data="stats.knowledge_bases" size="small">
            <el-table-column prop="name" label="知识库" min-width="180" />
            <el-table-column prop="document_count" label="文档" width="90" />
            <el-table-column prop="chunk_count" label="Chunk" width="90" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="9">
        <el-card shadow="never" class="boundary-card">
          <template #header><strong>当前权限边界</strong></template>
          <p v-if="auth.isAdmin">你可以管理全部用户、角色、知识库和系统级配置。</p>
          <p v-else>你可以进入知识库工作台，维护低于自身等级的知识库；不能修改用户角色或系统权限。</p>
          <el-button type="primary" plain @click="$router.push('/knowledge-bases')">进入知识库工作台</el-button>
          <el-button v-if="auth.isAdmin" @click="$router.push('/admin/users')">管理用户与角色</el-button>
        </el-card>
      </el-col>
    </el-row>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { listUsers } from "../../api/identity/admin";
import { getStats } from "../../api/knowledge/knowledgeBases";
import { useAuthStore } from "../../stores/auth";
import type { AuthUser, Stats } from "../../types";

const auth = useAuthStore();
const loading = ref(false);
const users = ref<AuthUser[]>([]);
const stats = ref<Stats>({
  document_count: 0,
  chunk_count: 0,
  storage_size: 0,
  knowledge_base_count: 0,
  vector_dim: 1024,
  knowledge_bases: [],
});

async function load() {
  loading.value = true;
  try {
    const tasks: Promise<unknown>[] = [getStats().then((value) => { stats.value = value; })];
    if (auth.isAdmin) tasks.push(listUsers().then((value) => { users.value = value; }));
    await Promise.all(tasks);
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.overview { max-width: 1200px; margin: 0 auto; }
.page-heading { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-heading h1 { font-size: 24px; margin: 0 0 6px; }
.page-heading p { margin: 0; color: var(--el-text-color-secondary); }
.metrics { margin-bottom: 16px; }
.metric { min-height: 76px; display: flex; flex-direction: column; justify-content: center; }
.metric strong { font-size: 28px; line-height: 1; }
.metric span { margin-top: 10px; color: var(--el-text-color-secondary); font-size: 13px; }
.boundary-card p { color: var(--el-text-color-regular); line-height: 1.8; min-height: 64px; }
.boundary-card .el-button + .el-button { margin-left: 8px; }
@media (max-width: 1199px) { .boundary-card { margin-top: 16px; } }
</style>
