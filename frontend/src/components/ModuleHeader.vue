<template>
  <header class="module-header">
    <div class="brand" @click="router.push('/chat')">
      <span class="brand-mark">AI</span>
      <span class="brand-copy">
        <strong>实验室 AI 助手</strong>
        <small>{{ sectionName }}</small>
      </span>
    </div>

    <nav class="module-nav" aria-label="功能区域">
      <el-button :type="section === 'chat' ? 'primary' : 'default'" @click="router.push('/chat')">
        智能问答
      </el-button>
      <el-button
        v-if="auth.isContentManager"
        :type="section === 'knowledge' ? 'primary' : 'default'"
        @click="router.push('/knowledge-bases')"
      >
        知识库工作台
      </el-button>
      <el-button
        v-if="auth.isContentManager"
        :type="section === 'admin' ? 'primary' : 'default'"
        @click="router.push('/admin')"
      >
        系统管理
      </el-button>
    </nav>

    <div class="identity">
      <template v-if="auth.user">
        <el-tag size="small" effect="plain">{{ auth.roleLabel }}</el-tag>
        <el-button link @click="router.push('/profile')">{{ auth.user.nickname }}</el-button>
        <el-button link type="danger" @click="signOut">退出</el-button>
      </template>
      <template v-else>
        <span class="guest-label">访客模式</span>
        <el-button type="primary" link @click="router.push('/login')">登录 / 注册</el-button>
      </template>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { ElMessage } from "element-plus";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const props = defineProps<{ section: "chat" | "knowledge" | "admin" }>();
const emit = defineEmits<{ signedOut: [] }>();
const router = useRouter();
const auth = useAuthStore();
const sectionName = computed(() => ({
  chat: "智能问答",
  knowledge: "知识库工作台",
  admin: "系统管理",
})[props.section]);

async function signOut() {
  try {
    await auth.logout();
    emit("signedOut");
    await router.replace("/chat");
    ElMessage.success("已退出登录");
  } catch (error) {
    ElMessage.error((error as Error).message);
  }
}
</script>

<style scoped>
.module-header {
  height: 64px;
  padding: 0 22px;
  display: grid;
  grid-template-columns: minmax(210px, 1fr) auto minmax(210px, 1fr);
  align-items: center;
  gap: 20px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: rgba(255, 255, 255, 0.96);
  box-sizing: border-box;
}
.brand { display: flex; align-items: center; gap: 10px; cursor: pointer; width: fit-content; }
.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: white;
  font-size: 13px;
  font-weight: 700;
  background: linear-gradient(135deg, var(--el-color-primary), #7a7cff);
}
.brand-copy { display: flex; flex-direction: column; line-height: 1.25; }
.brand-copy strong { font-size: 15px; }
.brand-copy small { color: var(--el-text-color-secondary); margin-top: 2px; }
.module-nav { display: flex; gap: 4px; }
.module-nav .el-button + .el-button { margin-left: 0; }
.identity { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.guest-label { color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 900px) {
  .module-header { grid-template-columns: 1fr auto; padding: 0 12px; }
  .brand-copy small { display: none; }
  .module-nav { order: 3; grid-column: 1 / -1; display: none; }
  .identity { min-width: 0; }
}
</style>
