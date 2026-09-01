<template>
  <main class="page">
    <el-card class="card">
      <template #header><strong>{{ registerMode ? "注册账号" : "登录实验室助手" }}</strong></template>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item v-if="registerMode" label="昵称">
          <el-input v-model="nickname" maxlength="64" placeholder="怎么称呼你" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="字母、数字、点、下划线或短横线" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" show-password placeholder="至少 8 位" @keyup.enter="submit" />
        </el-form-item>
        <el-form-item v-if="registerMode" label="邮箱（可选）">
          <el-input v-model="email" placeholder="用于后续联系，不填也可以" />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width:100%" @click="submit">
          {{ registerMode ? "注册并登录" : "登录" }}
        </el-button>
      </el-form>
      <div class="switch"><el-button link @click="registerMode = !registerMode">{{ registerMode ? "已有账号，去登录" : "没有账号，去注册" }}</el-button></div>
      <div class="switch"><el-button link @click="$router.push('/chat')">先以访客身份浏览</el-button></div>
    </el-card>
  </main>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const router = useRouter();
const auth = useAuthStore();
const registerMode = ref(false);
const loading = ref(false);
const username = ref("");
const password = ref("");
const nickname = ref("");
const email = ref("");

async function submit() {
  if (!username.value.trim() || !password.value) return ElMessage.warning("请填写用户名和密码");
  if (registerMode.value && !nickname.value.trim()) return ElMessage.warning("请填写昵称");
  loading.value = true;
  try {
    if (registerMode.value) {
      await auth.register({ username: username.value, password: password.value, nickname: nickname.value, email: email.value || undefined });
    } else {
      await auth.login(username.value, password.value);
    }
    ElMessage.success("登录成功");
    router.replace("/chat");
  } catch (err) {
    ElMessage.error((err as Error).message);
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.page { height: 100%; display: grid; place-items: center; background: var(--el-fill-color-light); }
.card { width: min(420px, calc(100vw - 32px)); }
.switch { text-align: center; margin-top: 8px; }
</style>
